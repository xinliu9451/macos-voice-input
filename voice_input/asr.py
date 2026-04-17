"""ASR recognition engine module.

封装 sherpa_onnx SenseVoice OfflineRecognizer + Silero VAD 为可复用的 ASREngine。
复用 StreamingAsr/infer.py 中的识别流水线（不修改 infer.py 本身）。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000  # sherpa_onnx SenseVoice 固定 16kHz
DEFAULT_MODEL_DIR = "StreamingAsr/model/"
INTERMEDIATE_UPDATE_INTERVAL = 0.2  # 中间结果刷新间隔（秒），对应 200ms


class ASREngine:
    """基于 sherpa_onnx 的 SenseVoice + Silero VAD 流式识别引擎。

    线程模型：
      - 录音线程：从 sounddevice 采集 PCM，写入 samples_queue
      - 处理线程：从 samples_queue 取音频，喂给 VAD + OfflineRecognizer，更新中间/最终文本
      - 锁 _lock 保护 _intermediate_text / _finalized_segments，保证 get_intermediate_text 线程安全
    """

    def __init__(
        self,
        model_dir: str = DEFAULT_MODEL_DIR,
        num_threads: int = 2,
        on_segment_finalized: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._model_dir = Path(model_dir)
        self._num_threads = num_threads
        self._on_segment_finalized = on_segment_finalized

        self._model_path = self._model_dir / "model.int8.onnx"
        self._tokens_path = self._model_dir / "tokens.txt"
        self._vad_model_path = self._model_dir / "vad.onnx"

        # 启动阶段就校验模型文件，避免录音时才炸
        self._assert_model_files_exist()

        # 延迟导入 sherpa_onnx：保证 asr 模块 import 不影响无模型场景的单元检查
        import sherpa_onnx  # type: ignore

        self._sherpa_onnx = sherpa_onnx

        self._recognizer = self._create_recognizer()
        self._vad_config = self._create_vad_config()
        self._window_size: int = self._vad_config.silero_vad.window_size

        # 运行时状态（由 _lock 保护）
        self._lock = threading.Lock()
        self._samples_queue: "queue.Queue[np.ndarray]" = queue.Queue()
        self._stop_event = threading.Event()
        self._recording_thread: Optional[threading.Thread] = None
        self._processing_thread: Optional[threading.Thread] = None

        self._intermediate_text: str = ""
        self._finalized_segments: List[str] = []
        self._pushed_count: int = 0  # 已通过 on_segment_finalized 回调推送的段落数
        self._running: bool = False
        self._error_message: Optional[str] = None

        logger.info("ASREngine initialized (model_dir=%s)", self._model_dir)

    # ---------- public API ----------

    def start_recording(self) -> None:
        """启动录音 + 识别。非阻塞，录音在后台线程进行。"""
        with self._lock:
            if self._running:
                logger.warning("start_recording called while already recording; ignored")
                return
            self._stop_event.clear()
            self._intermediate_text = ""
            self._finalized_segments = []
            self._pushed_count = 0
            self._error_message = None
            self._drain_queue_locked()
            self._running = True

        self._recording_thread = threading.Thread(
            target=self._recording_loop, name="ASR-Recording", daemon=True
        )
        self._processing_thread = threading.Thread(
            target=self._processing_loop, name="ASR-Processing", daemon=True
        )
        self._recording_thread.start()
        self._processing_thread.start()
        logger.info("ASREngine recording started")

    def stop_recording(self) -> str:
        """停止录音，等待处理完成，返回最终识别文本。"""
        with self._lock:
            if not self._running:
                return ""
            self._stop_event.set()

        # 在锁外 join，避免死锁
        if self._recording_thread is not None:
            self._recording_thread.join(timeout=2.0)
        if self._processing_thread is not None:
            self._processing_thread.join(timeout=5.0)

        with self._lock:
            self._running = False
            # 仅返回未通过 on_segment_finalized 回调推送过的尾音文本
            unpushed = self._finalized_segments[self._pushed_count:]
            if unpushed:
                final_text = "".join(unpushed).strip()
            else:
                final_text = self._intermediate_text.strip()

        logger.info(
            "ASREngine recording stopped, final_text_length=%d", len(final_text)
        )
        return final_text

    def get_intermediate_text(self) -> str:
        """返回当前中间识别结果（线程安全）。"""
        with self._lock:
            joined = "".join(self._finalized_segments)
            if self._intermediate_text:
                return (joined + self._intermediate_text).strip()
            return joined.strip()

    def get_error(self) -> Optional[str]:
        """返回最近的错误信息（线程安全）。无错误时返回 None。"""
        with self._lock:
            return self._error_message

    # ---------- internals ----------

    def _set_error(self, msg: str) -> None:
        """设置错误信息（线程安全）。"""
        with self._lock:
            self._error_message = msg

    def _assert_model_files_exist(self) -> None:
        missing: List[str] = []
        for path, name in (
            (self._model_path, "model.int8.onnx"),
            (self._tokens_path, "tokens.txt"),
            (self._vad_model_path, "vad.onnx"),
        ):
            if not path.is_file():
                missing.append(f"  - {name}: {path}")
        if missing:
            joined = "\n".join(missing)
            raise FileNotFoundError(
                "ASR 模型文件缺失，请确认以下文件位于 "
                f"{self._model_dir.resolve()} 下：\n{joined}"
            )

    def _create_recognizer(self):
        return self._sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(self._model_path),
            tokens=str(self._tokens_path),
            num_threads=self._num_threads,
            use_itn=True,  # 启用逆文本标准化（含标点）
            debug=False,
        )

    def _create_vad_config(self):
        config = self._sherpa_onnx.VadModelConfig()
        config.silero_vad.model = str(self._vad_model_path)
        config.silero_vad.threshold = 0.5
        config.silero_vad.min_silence_duration = 0.1
        config.silero_vad.min_speech_duration = 0.25
        config.silero_vad.max_speech_duration = 8
        config.sample_rate = SAMPLE_RATE
        return config

    def _drain_queue_locked(self) -> None:
        """调用方必须持锁；清空 samples_queue 中的残留音频。"""
        while not self._samples_queue.empty():
            try:
                self._samples_queue.get_nowait()
            except queue.Empty:
                break

    def _recording_loop(self) -> None:
        """采音线程：16kHz 单声道 float32 → samples_queue。"""
        try:
            import sounddevice as sd  # type: ignore
        except ImportError:
            logger.error(
                "sounddevice 未安装，无法录音。请执行 pip install sounddevice"
            )
            self._stop_event.set()
            self._set_error("sounddevice 未安装，请执行 pip install sounddevice")
            return

        samples_per_read = int(0.1 * SAMPLE_RATE)  # 100ms 一帧
        try:
            with sd.InputStream(
                channels=1, dtype="float32", samplerate=SAMPLE_RATE
            ) as stream:
                while not self._stop_event.is_set():
                    samples, _ = stream.read(samples_per_read)
                    samples = np.copy(samples.reshape(-1))
                    self._samples_queue.put(samples)
        except sd.PortAudioError as exc:
            err_str = str(exc).lower()
            if "no" in err_str and "device" in err_str:
                msg = (
                    "未检测到麦克风设备，请检查麦克风是否已连接。"
                    "如果使用外接麦克风，请确认设备已被 macOS 识别。"
                )
            elif "permission" in err_str or "not allowed" in err_str:
                msg = (
                    "麦克风权限被拒绝，请前往 System Preferences > Privacy & Security > "
                    "Microphone，将运行本程序的终端（或 Python 解释器）加入列表并勾选允许，"
                    "然后重启程序。"
                )
            else:
                msg = f"麦克风错误：{exc}。请检查麦克风权限和设备连接。"
            logger.error(msg)
            self._stop_event.set()
            self._set_error(msg)
        except Exception as exc:
            msg = f"录音异常：{exc}。请检查麦克风权限和设备连接。"
            logger.exception("Recording loop failed: %s", exc)
            self._stop_event.set()
            self._set_error(msg)

    def _processing_loop(self) -> None:
        """处理线程：VAD + 识别 + 刷新中间/最终文本。"""
        vad = self._sherpa_onnx.VoiceActivityDetector(
            self._vad_config, buffer_size_in_seconds=100
        )

        buffer = np.array([], dtype=np.float32)
        offset = 0
        started = False
        started_time: Optional[float] = None
        last_update_time = 0.0

        try:
            while not (self._stop_event.is_set() and self._samples_queue.empty()):
                try:
                    samples = self._samples_queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                buffer = np.concatenate([buffer, samples])
                while offset + self._window_size < len(buffer):
                    vad.accept_waveform(buffer[offset : offset + self._window_size])
                    if not started and vad.is_speech_detected():
                        started = True
                        started_time = time.time()
                    offset += self._window_size

                if not started:
                    # 无语音时滑动窗口，控制 buffer 体积
                    if len(buffer) > 10 * self._window_size:
                        offset -= len(buffer) - 10 * self._window_size
                        buffer = buffer[-10 * self._window_size :]

                now = time.time()
                if (
                    started
                    and started_time is not None
                    and now - last_update_time >= INTERMEDIATE_UPDATE_INTERVAL
                ):
                    text = self._decode_buffer(buffer)
                    with self._lock:
                        self._intermediate_text = text
                    last_update_time = now

                # VAD 切出一个完整句子 → 作为已完结片段
                while not vad.empty():
                    seg_stream = self._recognizer.create_stream()
                    seg_stream.accept_waveform(SAMPLE_RATE, vad.front.samples)
                    vad.pop()
                    self._recognizer.decode_stream(seg_stream)
                    text = seg_stream.result.text.strip()
                    with self._lock:
                        if text:
                            self._finalized_segments.append(text)
                        self._intermediate_text = ""

                    # 触发 on_segment_finalized 回调（在处理线程中同步调用）
                    if text and self._on_segment_finalized is not None:
                        try:
                            self._on_segment_finalized(text)
                            with self._lock:
                                self._pushed_count = len(self._finalized_segments)
                        except Exception as cb_exc:
                            logger.exception(
                                "on_segment_finalized callback failed: %s", cb_exc
                            )

                    buffer = np.array([], dtype=np.float32)
                    offset = 0
                    started = False
                    started_time = None

            # 停止时 flush 未完结的尾音
            self._flush_tail(vad, buffer, started)
        except Exception as exc:
            msg = f"语音识别处理异常：{exc}"
            logger.exception("Processing loop failed: %s", exc)
            self._set_error(msg)

    def _decode_buffer(self, buffer: np.ndarray) -> str:
        if len(buffer) == 0:
            return ""
        stream = self._recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, buffer)
        self._recognizer.decode_stream(stream)
        return stream.result.text.strip()

    def _flush_tail(self, vad, buffer: np.ndarray, started: bool) -> None:
        # 录音停止后，当前 buffer 中可能还有未被 VAD 切出的语音
        if started and len(buffer) > 0:
            text = self._decode_buffer(buffer)
            with self._lock:
                if text:
                    self._finalized_segments.append(text)
                self._intermediate_text = ""
        while not vad.empty():
            stream = self._recognizer.create_stream()
            stream.accept_waveform(SAMPLE_RATE, vad.front.samples)
            vad.pop()
            self._recognizer.decode_stream(stream)
            text = stream.result.text.strip()
            with self._lock:
                if text:
                    self._finalized_segments.append(text)
