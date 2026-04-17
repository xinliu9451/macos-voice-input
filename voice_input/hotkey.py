"""Global hotkey listener module.

监听全局快捷键（默认 Ctrl+Shift+Space），以 toggle 方式切换录音状态：
按一次触发 on_recording_start，再按一次触发 on_recording_stop。

macOS 说明：
pynput 在 macOS 上需要"辅助功能"权限（System Preferences >
Privacy & Security > Accessibility），否则收不到系统级按键事件。
权限缺失时，本模块会通过 logging 输出清晰提示。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)


DEFAULT_HOTKEY = "<ctrl>+<shift>+<space>"

PERMISSION_HINT = (
    "全局快捷键监听可能无法收到按键事件。macOS 需要『辅助功能 (Accessibility)』权限，"
    "请前往 System Preferences > Privacy & Security > Accessibility，"
    "将运行本程序的终端（或 Python 解释器）加入列表并勾选允许，然后重启程序。"
)


class HotkeyListener:
    """Toggle 式全局快捷键监听器。

    - 构造函数接收 on_recording_start / on_recording_stop 两个回调
    - start() 在后台守护线程启动监听器（非阻塞）
    - stop() 停止监听器并清理状态
    - 每按一次快捷键，切换 recording 状态并触发对应回调
    """

    def __init__(
        self,
        on_recording_start: Callable[[], None],
        on_recording_stop: Callable[[], None],
        hotkey: str = DEFAULT_HOTKEY,
    ) -> None:
        if on_recording_start is None or on_recording_stop is None:
            raise ValueError(
                "HotkeyListener requires both on_recording_start and on_recording_stop"
            )
        self._on_start = on_recording_start
        self._on_stop = on_recording_stop
        self._hotkey = hotkey

        self._listener = None  # pynput.keyboard.GlobalHotKeys 实例
        self._state_lock = threading.Lock()
        self._is_recording = False
        self._started = False

    # ---------- public API ----------

    def start(self) -> None:
        """启动监听（非阻塞）。pynput 内部会在守护线程中运行键盘钩子。"""
        if self._started:
            logger.warning("HotkeyListener.start called while already running; ignored")
            return

        try:
            from pynput import keyboard  # type: ignore
        except ImportError:
            logger.error(
                "pynput 未安装，无法启动全局快捷键监听。请执行：pip install pynput"
            )
            return

        try:
            listener = keyboard.GlobalHotKeys({self._hotkey: self._on_toggle})
            # pynput 的 Listener 继承自 threading.Thread；显式设为 daemon，
            # 确保主线程退出时不会因监听线程残留而卡住。
            listener.daemon = True
            listener.start()
        except Exception as exc:
            # 常见原因：权限不足、不受支持的快捷键字符串、pyobjc 未正确安装
            logger.error(
                "启动全局快捷键监听失败（hotkey=%s）：%s\n%s",
                self._hotkey,
                exc,
                PERMISSION_HINT,
            )
            return

        self._listener = listener
        self._started = True
        logger.info(
            "HotkeyListener started (hotkey=%s). 若按键未生效，请确认：%s",
            self._hotkey,
            PERMISSION_HINT,
        )

    def stop(self) -> None:
        """停止监听并重置状态。可重复调用。"""
        if not self._started and self._listener is None:
            return

        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception as exc:  # 停止失败不致命，打印后继续清理
                logger.warning("停止全局快捷键监听时出错：%s", exc)

        self._listener = None
        self._started = False
        with self._state_lock:
            self._is_recording = False
        logger.info("HotkeyListener stopped")

    def is_recording(self) -> bool:
        """返回当前是否处于录音状态（线程安全）。"""
        with self._state_lock:
            return self._is_recording

    def is_running(self) -> bool:
        """返回监听器是否已启动。"""
        return self._started

    # ---------- internals ----------

    def _on_toggle(self) -> None:
        """快捷键回调：切换录音状态并派发对应回调。"""
        with self._state_lock:
            self._is_recording = not self._is_recording
            new_state = self._is_recording

        try:
            if new_state:
                logger.debug("Hotkey toggled -> RECORDING")
                self._on_start()
            else:
                logger.debug("Hotkey toggled -> IDLE")
                self._on_stop()
        except Exception as exc:
            # 回调异常不应打断监听线程；回滚状态以便下次按键恢复
            logger.exception("Hotkey callback raised: %s", exc)
            with self._state_lock:
                self._is_recording = not self._is_recording
