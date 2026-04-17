"""Clipboard and paste module.

US-005 实现：将识别结果写入系统剪贴板，然后模拟 Cmd+V 将文本粘贴到当前光标位置。

设计要点：
    - 使用 PyObjC 的 NSPasteboard.generalPasteboard() 写入剪贴板（declareTypes + setString）
    - 使用 Quartz 的 CGEventCreateKeyboardEvent 合成 Cmd 按下 + V 按下 + V 抬起 + Cmd 抬起
    - 空字符串 / None 直接 no-op，避免清空用户剪贴板
    - 剪贴板写入与按键注入之间固定延迟 PASTE_DELAY_SECONDS（80ms，位于 50-100ms 区间）
    - PyObjC/Quartz 不可用时退化为 no-op + warning，与 Overlay 模式保持一致
    - 不做剪贴板内容恢复（PRD Non-Goal）

权限说明：
    CGEvent 合成按键需要 macOS『辅助功能 (Accessibility)』权限。
    权限缺失时按键注入会失败（通常静默），本模块在 paste_text 时提示一次。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# 写入剪贴板后等待 80ms 再模拟 Cmd+V。
# 区间 50-100ms 是经验值：太短会在目标窗口前焦点/剪贴板未就绪，太长会影响体感。
PASTE_DELAY_SECONDS = 0.08

# virtual key code for 'V' on macOS（JIS/ANSI 键盘相同，来自 HIToolbox/Events.h kVK_ANSI_V）
V_KEY_CODE = 9

# Quartz CGEventFlags.kCGEventFlagMaskCommand 对应的位
_CG_EVENT_FLAG_MASK_COMMAND = 1 << 20

PERMISSION_HINT = (
    "Cmd+V 自动粘贴需要 macOS『辅助功能 (Accessibility)』权限。"
    "请前往 System Preferences > Privacy & Security > Accessibility，"
    "将运行本程序的终端（或 Python 解释器）加入列表并勾选允许。"
)


class Clipboard:
    """剪贴板写入 + Cmd+V 按键合成的封装。

    线程模型：
        - paste_text 可从任意线程调用（锁保护内部 Cocoa 调用序列化）
        - 实际粘贴按键注入会落在调用线程（CGEvent 不要求主线程，但要求 Accessibility 权限）
    """

    def __init__(self, paste_delay: float = PASTE_DELAY_SECONDS) -> None:
        # 允许调用方调整延迟，但限定在 50-100ms 区间，避免 AC 偏离
        self._paste_delay = max(0.05, min(0.10, float(paste_delay)))
        self._lock = threading.Lock()

        # PyObjC 模块引用（延迟加载，保证无 PyObjC 环境下模块可 import）
        self._AppKit = None
        self._Foundation = None
        self._Quartz = None
        self._frameworks_available = False
        self._permission_hint_printed = False

        self._try_init_frameworks()

    # ---------- public API ----------

    def paste_text(self, text: str) -> None:
        """将 text 写入系统剪贴板并模拟 Cmd+V 粘贴。

        - text 为空字符串或 None 时直接返回（AC：空识别结果不执行粘贴）
        - 写入剪贴板后等待 paste_delay 秒（50-100ms），再注入 Cmd+V
        - 粘贴完成后不恢复原剪贴板内容（PRD Non-Goal）
        - 框架不可用时打印 warning 后返回
        """
        if text is None or text == "":
            logger.debug("paste_text called with empty/None text; skipping paste.")
            return

        if not self._frameworks_available:
            logger.warning(
                "paste_text called but AppKit/Quartz unavailable; skipping. "
                "请执行 pip install pyobjc-framework-Cocoa pyobjc-framework-Quartz"
            )
            return

        with self._lock:
            wrote = self._write_pasteboard(text)
            if not wrote:
                return
            # 让目标应用的前台进程/剪贴板订阅者有机会同步到新内容，再注入按键
            time.sleep(self._paste_delay)
            self._send_cmd_v()

    # ---------- internals ----------

    def _try_init_frameworks(self) -> None:
        try:
            import AppKit  # type: ignore
            import Foundation  # type: ignore
            import Quartz  # type: ignore

            self._AppKit = AppKit
            self._Foundation = Foundation
            self._Quartz = Quartz
            self._frameworks_available = True
        except ImportError as exc:
            logger.warning(
                "Clipboard init: PyObjC 导入失败 (%s)。粘贴功能将不可用。"
                "请执行 pip install pyobjc-framework-Cocoa pyobjc-framework-Quartz",
                exc,
            )
            self._frameworks_available = False

    def _write_pasteboard(self, text: str) -> bool:
        """将 text 写入 NSPasteboard.generalPasteboard()。成功返回 True。"""
        AppKit = self._AppKit
        try:
            pasteboard = AppKit.NSPasteboard.generalPasteboard()
            # declareTypes 会清空现有内容并重新声明类型（标准 NSPasteboard 写入流程）
            pasteboard.declareTypes_owner_([AppKit.NSPasteboardTypeString], None)
            ok = pasteboard.setString_forType_(text, AppKit.NSPasteboardTypeString)
            if not ok:
                logger.error("NSPasteboard.setString_forType_ 返回 False，写入失败")
                return False
            logger.debug("Clipboard: wrote %d chars to general pasteboard", len(text))
            return True
        except Exception as exc:
            logger.exception("Clipboard 写入剪贴板失败: %s", exc)
            return False

    def _send_cmd_v(self) -> None:
        """合成并派发 Cmd+V 的 KeyDown/KeyUp 事件。"""
        Quartz = self._Quartz
        try:
            # 使用 HID 事件源，事件从系统级注入，绝大多数应用会接收到
            source = Quartz.CGEventSourceCreate(
                Quartz.kCGEventSourceStateHIDSystemState
            )

            # 注意：合成 Cmd+V 的典型做法是让 V 的 keydown 自带 Command flag，
            # 而不是显式的 Command keydown——后者在部分应用下会被视作单独的
            # 修饰键按下并触发菜单焦点。
            v_down = Quartz.CGEventCreateKeyboardEvent(source, V_KEY_CODE, True)
            v_up = Quartz.CGEventCreateKeyboardEvent(source, V_KEY_CODE, False)

            Quartz.CGEventSetFlags(v_down, _CG_EVENT_FLAG_MASK_COMMAND)
            Quartz.CGEventSetFlags(v_up, _CG_EVENT_FLAG_MASK_COMMAND)

            Quartz.CGEventPost(Quartz.kCGHIDEventTap, v_down)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, v_up)
            logger.debug("Clipboard: posted Cmd+V via CGEvent")
        except Exception as exc:
            # CGEvent 在 Accessibility 权限不足时通常静默失败，但保留日志路径
            logger.error("发送 Cmd+V 失败: %s", exc)
            if not self._permission_hint_printed:
                logger.error(PERMISSION_HINT)
                self._permission_hint_printed = True


def paste_text(text: str) -> None:
    """便捷函数：使用模块级单例 Clipboard 粘贴 text。"""
    _default_clipboard().paste_text(text)


_default_instance: Optional[Clipboard] = None
_default_lock = threading.Lock()


def _default_clipboard() -> Clipboard:
    global _default_instance
    if _default_instance is not None:
        return _default_instance
    with _default_lock:
        if _default_instance is None:
            _default_instance = Clipboard()
        return _default_instance
