"""macOS menu bar icon and status display using rumps.

US-006 实现：菜单栏图标与状态显示。

设计要点：
    - rumps.App 创建菜单栏应用，主线程运行（macOS AppKit 要求）
    - 待命状态菜单栏标题 '🎙'，录音中状态 '🔴'
    - 菜单项：'开始/停止录音'、separator、'退出'
    - set_recording_state() 供外部（hotkey / app.py）切换状态图标
    - rumps 的 run loop 阻塞主线程；其他模块需在子线程启动

注意：rumps 未安装时模块仍可 import，MenuBarApp 退化为 no-op 并打印警告。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

ICON_IDLE = "🎙"
ICON_RECORDING = "🔴"
MENU_TOGGLE = "开始/停止录音"
MENU_QUIT = "退出"


class MenuBarApp:
    """macOS menu bar application wrapping ``rumps.App``.

    Parameters
    ----------
    on_toggle_recording:
        Callback fired when user clicks '开始/停止录音'. May be ``None``
        (menu item still appears but does nothing).

    Thread model
    ------------
    ``run()`` blocks on the main thread (macOS AppKit requirement).  All other
    modules (ASR, hotkey, overlay …) must be started on background threads
    *before* calling ``run()``.
    """

    def __init__(
        self,
        on_toggle_recording: Optional[Callable[[], None]] = None,
    ) -> None:
        self._on_toggle_recording = on_toggle_recording
        self._is_recording = False
        self._lock = threading.Lock()

        # rumps handles (lazily initialised in _build_app)
        self._app = None  # type: Optional[object]
        self._rumps = None
        self._rumps_available = False

        self._try_init_rumps()

    # ---------- public API ----------

    def run(self) -> None:
        """Start the rumps run loop. **Blocks the calling (main) thread.**"""
        if not self._rumps_available:
            logger.warning(
                "MenuBarApp.run() called but rumps is unavailable; "
                "the menu bar icon will not appear. "
                "Please install rumps: pip install rumps"
            )
            return
        self._build_app()
        if self._app is not None:
            logger.info("MenuBarApp: starting rumps run loop on main thread")
            self._app.run()

    def set_recording_state(self, is_recording: bool) -> None:
        """Switch the menu bar icon between idle and recording states.

        Thread-safe — may be called from any thread.
        """
        with self._lock:
            self._is_recording = bool(is_recording)
            title = ICON_RECORDING if self._is_recording else ICON_IDLE
        if self._app is not None:
            try:
                self._app.title = title
            except Exception:
                logger.debug("set_recording_state: could not update title", exc_info=True)

    def quit(self) -> None:
        """Programmatically quit the rumps application."""
        if not self._rumps_available:
            return
        try:
            self._rumps.quit_application()
        except Exception:
            logger.debug("MenuBarApp.quit() failed", exc_info=True)

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    # ---------- internals ----------

    def _try_init_rumps(self) -> None:
        try:
            import rumps  # type: ignore
            self._rumps = rumps
            self._rumps_available = True
        except ImportError as exc:
            logger.warning(
                "MenuBarApp init: rumps 导入失败 (%s)。菜单栏功能将不可用。"
                "请执行 pip install rumps",
                exc,
            )
            self._rumps_available = False

    def _build_app(self) -> None:
        """Construct the rumps.App instance with menu items."""
        if self._app is not None:
            return
        if not self._rumps_available:
            return

        rumps = self._rumps

        app = rumps.App(
            name="VoiceInput",
            title=ICON_IDLE,
            quit_button=None,  # we add our own quit item
        )

        toggle_item = rumps.MenuItem(MENU_TOGGLE, callback=self._on_menu_toggle)
        quit_item = rumps.MenuItem(MENU_QUIT, callback=self._on_menu_quit)

        app.menu = [
            toggle_item,
            rumps.separator,
            quit_item,
        ]

        self._app = app
        logger.debug("MenuBarApp: rumps.App built (title=%s)", app.title)

    def _on_menu_toggle(self, _sender) -> None:
        """Handle '开始/停止录音' menu click."""
        if self._on_toggle_recording is not None:
            try:
                self._on_toggle_recording()
            except Exception:
                logger.exception("MenuBarApp: on_toggle_recording callback failed")

    def _on_menu_quit(self, _sender) -> None:
        """Handle '退出' menu click."""
        logger.info("MenuBarApp: user clicked quit")
        self._rumps.quit_application()
