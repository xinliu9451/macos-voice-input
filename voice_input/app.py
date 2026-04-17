"""macOS Voice Input Tool - main application entry point.

US-007 实现：模块集成与端到端流程。

启动流程：
    1. 解析 CLI 参数（--model-dir, --verbose, --check）
    2. 子线程加载 ASR 模型，菜单栏显示加载状态
    3. 加载完成后启动 HotkeyListener，进入待命状态
    4. 用户按 Ctrl+Shift+Space → 开始录音 → 浮窗实时更新 → 再按 → 停止录音 → 粘贴文本
    5. rumps 主线程 run loop 保持应用运行
    6. Ctrl+C 或菜单栏「退出」优雅终止

线程模型：
    - 主线程：rumps run loop（macOS AppKit 要求）
    - 子线程 1：ASR 模型加载（启动阶段一次性）
    - 子线程 2/3：ASREngine 录音线程 + 处理线程（录音期间）
    - 子线程 4：HotkeyListener（pynput daemon thread）
    - 子线程 5：中间结果轮询线程（录音期间）
"""

import argparse
import logging
import signal
import sys
import threading
import time

logger = logging.getLogger(__name__)


def parse_args(argv=None):
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        prog="voice_input",
        description="macOS system-level voice input tool based on sherpa_onnx SenseVoice.",
    )
    parser.add_argument(
        "--model-dir",
        type=str,
        default="StreamingAsr/model/",
        help="Path to the directory containing the ASR model files.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG level logging.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run a scaffold self-check (initialize and exit immediately).",
    )
    return parser.parse_args(argv)


def configure_logging(verbose: bool) -> None:
    """Configure root logger."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


class VoiceInputApp:
    """端到端语音输入应用，集成 ASR + Hotkey + Overlay + Clipboard + MenuBar。"""

    def __init__(self, model_dir: str) -> None:
        self._model_dir = model_dir
        self._asr_engine = None
        self._hotkey_listener = None
        self._overlay = None
        self._clipboard = None
        self._menubar = None

        self._model_loaded = threading.Event()
        self._poll_thread = None
        self._poll_stop = threading.Event()
        self._is_recording = False  # 录音状态标志，用于 on_segment_finalized 门控
        self._shutting_down = False

    def run(self) -> int:
        """启动应用，阻塞在 rumps run loop 上。返回退出码。"""
        from voice_input.overlay import Overlay
        from voice_input.clipboard import Clipboard
        from voice_input.menubar import MenuBarApp

        # 初始化 Overlay 和 Clipboard（不依赖模型）
        self._overlay = Overlay()
        self._clipboard = Clipboard()

        # 初始化 MenuBarApp
        self._menubar = MenuBarApp(on_toggle_recording=self._on_menu_toggle)

        # 子线程加载模型
        load_thread = threading.Thread(
            target=self._load_model, name="ModelLoader", daemon=True
        )
        load_thread.start()

        # 注册 SIGINT handler 以便 Ctrl+C 优雅退出
        signal.signal(signal.SIGINT, self._signal_handler)

        logger.info("VoiceInputApp: starting main run loop")
        # rumps.run() 阻塞主线程
        self._menubar.run()

        # rumps 退出后，清理所有资源
        self._shutdown()
        return 0

    # ---------- 模型加载 ----------

    def _load_model(self) -> None:
        """子线程：加载 ASR 模型并初始化 HotkeyListener。"""
        logger.info("开始加载 ASR 模型 (model_dir=%s)...", self._model_dir)
        try:
            from voice_input.asr import ASREngine

            self._asr_engine = ASREngine(
                model_dir=self._model_dir,
                on_segment_finalized=self._on_segment_finalized,
            )
            self._model_loaded.set()
            logger.info("ASR 模型加载完成")

            # 模型加载成功后启动快捷键监听
            self._start_hotkey_listener()

        except FileNotFoundError as exc:
            logger.error("模型文件缺失，无法启动：%s", exc)
            # 通知用户并退出
            if self._menubar is not None:
                self._menubar.quit()
        except Exception as exc:
            logger.exception("ASR 模型加载失败: %s", exc)
            if self._menubar is not None:
                self._menubar.quit()

    def _start_hotkey_listener(self) -> None:
        """初始化并启动全局快捷键监听器。"""
        from voice_input.hotkey import HotkeyListener

        self._hotkey_listener = HotkeyListener(
            on_recording_start=self._on_recording_start,
            on_recording_stop=self._on_recording_stop,
        )
        self._hotkey_listener.start()
        logger.info("全局快捷键监听已启动，按 Ctrl+Shift+Space 开始/停止录音")

    # ---------- 录音回调 ----------

    def _on_recording_start(self) -> None:
        """快捷键触发：开始录音。"""
        if self._asr_engine is None or not self._model_loaded.is_set():
            logger.warning("模型尚未加载完成，忽略录音请求")
            return

        logger.info("开始录音")
        self._is_recording = True

        # 更新菜单栏状态
        if self._menubar is not None:
            self._menubar.set_recording_state(True)

        # 显示浮窗
        if self._overlay is not None:
            self._overlay.show()

        # 启动 ASR 录音
        try:
            self._asr_engine.start_recording()
        except Exception as exc:
            logger.exception("启动录音失败: %s", exc)
            self._recover_to_idle(f"启动录音失败：{exc}")
            return

        # 启动中间结果轮询线程
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_intermediate_text, name="TextPoller", daemon=True
        )
        self._poll_thread.start()

    def _on_recording_stop(self) -> None:
        """快捷键触发：停止录音并粘贴尾音结果。"""
        if self._asr_engine is None:
            return

        logger.info("停止录音")
        self._is_recording = False

        # 停止轮询线程
        self._poll_stop.set()

        # 更新菜单栏状态
        if self._menubar is not None:
            self._menubar.set_recording_state(False)

        # 停止录音并获取尾音文本（不含已通过回调推送的段落）
        try:
            tail_text = self._asr_engine.stop_recording()
        except Exception as exc:
            logger.exception("停止录音时发生异常: %s", exc)
            self._recover_to_idle(f"语音识别异常：{exc}")
            return

        # 检查录音过程中是否有错误（如麦克风掉线、ASR 引擎异常）
        error = self._asr_engine.get_error()
        if error:
            logger.error("录音过程中发生错误: %s", error)
            self._recover_to_idle(error)
            return

        logger.info("尾音识别结果: %s", tail_text)

        # 更新浮窗显示最终结果（显示完整识别历史）
        if self._overlay is not None:
            full_text = self._asr_engine.get_intermediate_text()
            if full_text:
                self._overlay.update_text(full_text)

        # 粘贴尾音文本（已通过回调推送的段落不重复粘贴）
        if self._clipboard is not None and tail_text:
            self._clipboard.paste_text(tail_text)

        # 延迟隐藏浮窗（让用户看到最终结果）
        threading.Thread(
            target=self._delayed_hide_overlay, name="OverlayHider", daemon=True
        ).start()

    def _on_segment_finalized(self, text: str) -> None:
        """ASR 处理线程回调：VAD 切出完整句子后立即粘贴。

        在 ASR 处理线程中同步调用，需线程安全。
        shutdown/recovery 状态下跳过粘贴。
        """
        if not self._is_recording or self._shutting_down:
            logger.debug("跳过分段粘贴（录音已停止或正在关闭）: %s", text)
            return
        logger.info("分段粘贴: %s", text)
        if self._clipboard is not None:
            self._clipboard.paste_text(text)

    def _on_menu_toggle(self) -> None:
        """菜单栏「开始/停止录音」回调。"""
        if self._hotkey_listener is None:
            logger.warning("快捷键监听器尚未初始化")
            return
        # 复用 hotkey 的 toggle 逻辑
        self._hotkey_listener._on_toggle()

    # ---------- 中间结果轮询 ----------

    def _poll_intermediate_text(self) -> None:
        """轮询线程：每 200ms 更新浮窗的中间识别结果，同时检测 ASR 错误。"""
        while not self._poll_stop.is_set():
            if self._asr_engine is not None:
                # 检查是否有录音/识别错误
                error = self._asr_engine.get_error()
                if error:
                    logger.error("轮询线程检测到 ASR 错误: %s", error)
                    self._poll_stop.set()
                    self._recover_to_idle(error)
                    return
                if self._overlay is not None:
                    text = self._asr_engine.get_intermediate_text()
                    if text:
                        self._overlay.update_text(text)
            self._poll_stop.wait(timeout=0.2)

    def _recover_to_idle(self, error_message: str) -> None:
        """ASR 异常后恢复到待命状态：浮窗显示错误、更新菜单栏、停止录音。"""
        logger.warning("恢复到待命状态: %s", error_message)
        self._is_recording = False

        # 更新菜单栏为待命
        if self._menubar is not None:
            self._menubar.set_recording_state(False)

        # 浮窗显示错误提示
        if self._overlay is not None:
            self._overlay.update_text(f"⚠ {error_message}")

        # 尝试停止 ASR（可能已经停了）
        if self._asr_engine is not None:
            try:
                self._asr_engine.stop_recording()
            except Exception:
                pass

        # 重置 hotkey toggle 状态
        if self._hotkey_listener is not None:
            with self._hotkey_listener._state_lock:
                self._hotkey_listener._is_recording = False

        # 延迟隐藏错误浮窗（给用户 3 秒阅读错误信息）
        threading.Thread(
            target=self._delayed_hide_overlay,
            args=(3.0,),
            name="ErrorOverlayHider",
            daemon=True,
        ).start()

    def _delayed_hide_overlay(self, delay: float = 1.0) -> None:
        """延迟指定秒数后隐藏浮窗。"""
        time.sleep(delay)
        if self._overlay is not None:
            self._overlay.hide()

    # ---------- 优雅退出 ----------

    def _signal_handler(self, signum, frame) -> None:
        """Ctrl+C 信号处理。"""
        logger.info("收到信号 %d，正在退出...", signum)
        if self._menubar is not None:
            self._menubar.quit()

    def _shutdown(self) -> None:
        """清理所有资源。"""
        if self._shutting_down:
            return
        self._shutting_down = True
        logger.info("VoiceInputApp: shutting down...")

        # 停止轮询
        self._poll_stop.set()

        # 停止快捷键监听
        if self._hotkey_listener is not None:
            self._hotkey_listener.stop()

        # 停止录音（如果正在进行）
        if self._asr_engine is not None:
            try:
                self._asr_engine.stop_recording()
            except Exception:
                pass

        # 隐藏浮窗
        if self._overlay is not None:
            self._overlay.hide()

        logger.info("VoiceInputApp: shutdown complete")


def main(argv=None) -> int:
    """Application entry point. Returns process exit code."""
    args = parse_args(argv)
    configure_logging(args.verbose)

    logger.info("voice_input started (model-dir=%s)", args.model_dir)

    if args.check:
        logger.info("Scaffold self-check complete. Exiting.")
        return 0

    app = VoiceInputApp(model_dir=args.model_dir)
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
