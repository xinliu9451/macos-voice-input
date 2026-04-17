"""Screen overlay for displaying real-time recognition results.

US-004 实现：基于 PyObjC NSPanel 的半透明顶部悬浮窗。

设计要点：
    - NSPanel 无边框 + 圆角 + 半透明深色背景 + 白色粗体文字
    - 窗口 level=NSFloatingWindowLevel，可跨 Space、不抢焦点、忽略鼠标事件
    - 屏幕顶部居中，宽度自适应文本长度，最大 80% 屏幕宽度
    - 所有 UI 更新通过 _OverlayController + performSelectorOnMainThread 派发到主线程
    - PyObjC/AppKit 不可用时模块仍可 import；show/hide/update_text 退化为 no-op 并打印告警

注意：本模块本身不启动 NSApplication run loop（那是 US-007 集成的责任）。
在主线程未进入 run loop 前，NSPanel 的 UI 更新可能不会立即刷新，但构造/方法调用是安全的。
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_INITIAL_TEXT = "正在聆听..."
DEFAULT_FONT_SIZE = 22.0  # > 18pt，满足 AC
MIN_WIDTH = 260.0
MAX_WIDTH_RATIO = 0.8
HORIZONTAL_PADDING = 32.0
VERTICAL_PADDING = 18.0
CORNER_RADIUS = 14.0
BACKGROUND_ALPHA = 0.78
TOP_MARGIN = 64.0


class Overlay:
    """Floating HUD window showing real-time ASR text near the top of the screen.

    Thread model:
        - `show()` / `hide()` / `update_text()` 可从任意线程调用
        - 内部通过 `_OverlayController` (NSObject 子类)
          的 `performSelectorOnMainThread_withObject_waitUntilDone_` 将所有 UI
          操作派发回主线程。
        - 若当前已在主线程（NSThread.isMainThread()），直接同步调用 selector，
          避免不必要的 round-trip。
    """

    def __init__(
        self,
        initial_text: str = DEFAULT_INITIAL_TEXT,
        font_size: float = DEFAULT_FONT_SIZE,
    ) -> None:
        self._initial_text = initial_text
        self._font_size = max(18.0, float(font_size))  # AC: >=18pt

        # AppKit handles（延迟到首次 _ensure_panel 时初始化）
        self._panel = None
        self._text_field = None
        self._content_view = None
        self._controller = None
        self._controller_cls = None

        self._visible = False
        self._lock = threading.Lock()

        # PyObjC 模块引用
        self._AppKit = None
        self._Foundation = None
        self._objc = None
        self._appkit_available = False

        self._try_init_appkit()

    # ---------- public API ----------

    def show(self) -> None:
        """Show the overlay at top-center of the screen with the initial text."""
        if not self._appkit_available:
            logger.warning(
                "Overlay.show() called but AppKit is unavailable; ignored."
            )
            return
        self._run_on_main("showOverlay:", None)

    def hide(self) -> None:
        """Hide the overlay (no-op if not visible or AppKit unavailable)."""
        if not self._appkit_available:
            return
        self._run_on_main("hideOverlay:", None)

    def update_text(self, text: str) -> None:
        """Update the displayed text. Thread-safe; marshals UI work to main thread."""
        if text is None:
            text = ""
        if not self._appkit_available:
            return
        self._run_on_main("updateText:", text)

    def is_visible(self) -> bool:
        """Return whether the overlay is currently considered visible."""
        return self._visible

    # ---------- main-thread dispatch targets (called from _OverlayController) ----------

    def _do_show(self) -> None:
        self._ensure_panel()
        if self._panel is None:
            return
        # AC: show() 初始显示“正在聆听...”
        self._apply_text(self._initial_text)
        self._panel.orderFrontRegardless()
        self._visible = True

    def _do_hide(self) -> None:
        if self._panel is None:
            return
        self._panel.orderOut_(None)
        self._visible = False

    def _do_update_text(self, text: str) -> None:
        self._ensure_panel()
        if self._panel is None:
            return
        self._apply_text(text)

    # ---------- internals ----------

    def _try_init_appkit(self) -> None:
        try:
            import AppKit  # type: ignore
            import Foundation  # type: ignore
            import objc  # type: ignore

            self._AppKit = AppKit
            self._Foundation = Foundation
            self._objc = objc
            self._appkit_available = True
        except ImportError as exc:
            logger.warning(
                "Overlay init: PyObjC 导入失败 (%s)。 浮窗功能将不可用。"
                "请执行 pip install pyobjc-framework-Cocoa",
                exc,
            )
            self._appkit_available = False

    def _get_controller_class(self):
        """Lazily build the NSObject subclass used for main-thread dispatch."""
        if self._controller_cls is not None:
            return self._controller_cls
        if not self._appkit_available:
            return None

        objc = self._objc
        Foundation = self._Foundation
        NSObject = Foundation.NSObject
        overlay_logger = logger

        class _OverlayController(NSObject):  # type: ignore[misc]
            def initWithOverlay_(self, overlay):  # noqa: N802 (ObjC naming)
                self = objc.super(_OverlayController, self).init()
                if self is None:
                    return None
                self._overlay = overlay
                return self

            def showOverlay_(self, _arg):  # noqa: N802
                try:
                    self._overlay._do_show()
                except Exception:  # pragma: no cover - defensive
                    overlay_logger.exception("Overlay showOverlay: failed")

            def hideOverlay_(self, _arg):  # noqa: N802
                try:
                    self._overlay._do_hide()
                except Exception:  # pragma: no cover - defensive
                    overlay_logger.exception("Overlay hideOverlay: failed")

            def updateText_(self, text):  # noqa: N802
                try:
                    self._overlay._do_update_text(
                        str(text) if text is not None else ""
                    )
                except Exception:  # pragma: no cover - defensive
                    overlay_logger.exception("Overlay updateText: failed")

        self._controller_cls = _OverlayController
        return _OverlayController

    def _ensure_controller(self) -> None:
        with self._lock:
            if self._controller is not None:
                return
            cls = self._get_controller_class()
            if cls is None:
                return
            self._controller = cls.alloc().initWithOverlay_(self)

    def _run_on_main(self, selector: str, arg) -> None:
        """Run the named selector on _OverlayController on the main thread."""
        self._ensure_controller()
        controller = self._controller
        if controller is None:
            return
        sel = selector.encode("ascii") if isinstance(selector, str) else selector
        try:
            if self._Foundation.NSThread.isMainThread():
                controller.performSelector_withObject_(sel, arg)
            else:
                controller.performSelectorOnMainThread_withObject_waitUntilDone_(
                    sel, arg, False
                )
        except Exception as exc:
            logger.exception("Overlay main-thread dispatch failed: %s", exc)

    def _ensure_panel(self) -> None:
        """Create NSPanel + NSTextField on the main thread (caller guarantees main)."""
        if self._panel is not None:
            return
        if not self._appkit_available:
            return

        AppKit = self._AppKit
        Foundation = self._Foundation

        # Ensure NSApplication exists (idempotent; safe even without run loop)
        AppKit.NSApplication.sharedApplication()

        screen_frame = self._main_screen_frame()

        initial_width = 360.0
        initial_height = self._compute_height()
        x = (
            screen_frame.origin.x
            + (screen_frame.size.width - initial_width) / 2.0
        )
        y = (
            screen_frame.origin.y
            + screen_frame.size.height
            - initial_height
            - TOP_MARGIN
        )

        rect = Foundation.NSMakeRect(x, y, initial_width, initial_height)

        style_mask = AppKit.NSWindowStyleMaskBorderless
        panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style_mask, AppKit.NSBackingStoreBuffered, False
        )
        panel.setOpaque_(False)
        panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        panel.setLevel_(AppKit.NSFloatingWindowLevel)
        panel.setIgnoresMouseEvents_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setMovableByWindowBackground_(False)
        panel.setHasShadow_(True)

        try:
            panel.setCollectionBehavior_(
                AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
                | AppKit.NSWindowCollectionBehaviorStationary
            )
        except AttributeError:
            # Older macOS frameworks may not expose all flags.
            pass

        content_view = AppKit.NSView.alloc().initWithFrame_(
            Foundation.NSMakeRect(0, 0, initial_width, initial_height)
        )
        content_view.setWantsLayer_(True)
        layer = content_view.layer()
        if layer is not None:
            bg = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.05, 0.05, 0.08, BACKGROUND_ALPHA
            )
            import Quartz  # type: ignore

            cg_color = Quartz.CGColorCreate(
                Quartz.CGColorSpaceCreateDeviceRGB(),
                (0.05, 0.05, 0.08, BACKGROUND_ALPHA),
            )
            layer.setBackgroundColor_(cg_color)
            layer.setCornerRadius_(CORNER_RADIUS)
            layer.setMasksToBounds_(True)

        text_field = AppKit.NSTextField.alloc().initWithFrame_(
            Foundation.NSMakeRect(
                HORIZONTAL_PADDING,
                VERTICAL_PADDING,
                initial_width - 2 * HORIZONTAL_PADDING,
                initial_height - 2 * VERTICAL_PADDING,
            )
        )
        text_field.setEditable_(False)
        text_field.setSelectable_(False)
        text_field.setBezeled_(False)
        text_field.setBordered_(False)
        text_field.setDrawsBackground_(False)
        text_field.setFont_(AppKit.NSFont.boldSystemFontOfSize_(self._font_size))
        text_field.setTextColor_(AppKit.NSColor.whiteColor())
        try:
            text_field.setAlignment_(AppKit.NSTextAlignmentCenter)
        except AttributeError:
            text_field.setAlignment_(2)  # fallback: NSCenterTextAlignment
        text_field.setStringValue_(self._initial_text)

        content_view.addSubview_(text_field)
        panel.setContentView_(content_view)

        self._panel = panel
        self._content_view = content_view
        self._text_field = text_field

        logger.debug(
            "Overlay NSPanel created at x=%.1f y=%.1f w=%.1f h=%.1f",
            x, y, initial_width, initial_height,
        )

    def _compute_height(self) -> float:
        # Panel height = font leading * 1.8 + vertical padding
        return max(56.0, self._font_size * 1.8 + 2 * VERTICAL_PADDING)

    def _main_screen_frame(self):
        Foundation = self._Foundation
        AppKit = self._AppKit
        screen = AppKit.NSScreen.mainScreen()
        if screen is None:
            return Foundation.NSMakeRect(0.0, 0.0, 1440.0, 900.0)
        return screen.frame()

    def _apply_text(self, text: str) -> None:
        if self._panel is None or self._text_field is None:
            return

        AppKit = self._AppKit
        Foundation = self._Foundation

        display = text if text else self._initial_text

        font = AppKit.NSFont.boldSystemFontOfSize_(self._font_size)

        # Measure text width via NSString.sizeWithAttributes_
        try:
            attrs = {AppKit.NSFontAttributeName: font}
            ns_string = Foundation.NSString.stringWithString_(display)
            measured = ns_string.sizeWithAttributes_(attrs)
            text_width = float(measured.width)
        except Exception:
            # Fallback heuristic if attributed measurement fails
            text_width = float(len(display)) * self._font_size * 0.6

        screen_frame = self._main_screen_frame()
        max_width = screen_frame.size.width * MAX_WIDTH_RATIO
        desired_width = min(
            max(MIN_WIDTH, text_width + 2 * HORIZONTAL_PADDING),
            max_width,
        )
        desired_height = self._compute_height()

        x = (
            screen_frame.origin.x
            + (screen_frame.size.width - desired_width) / 2.0
        )
        y = (
            screen_frame.origin.y
            + screen_frame.size.height
            - desired_height
            - TOP_MARGIN
        )

        new_panel_frame = Foundation.NSMakeRect(x, y, desired_width, desired_height)
        self._panel.setFrame_display_animate_(new_panel_frame, True, False)

        content_frame = Foundation.NSMakeRect(0, 0, desired_width, desired_height)
        self._content_view.setFrame_(content_frame)

        text_frame = Foundation.NSMakeRect(
            HORIZONTAL_PADDING,
            VERTICAL_PADDING,
            desired_width - 2 * HORIZONTAL_PADDING,
            desired_height - 2 * VERTICAL_PADDING,
        )
        self._text_field.setFrame_(text_frame)
        self._text_field.setStringValue_(display)
