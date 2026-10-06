"""Foreground Windows UI Automation backend; native imports are deliberately lazy."""
from __future__ import annotations

import hashlib
import json
import sys
import time
from typing import Any

from ..errors import (
    ActionNotOffered,
    ElementNotFound,
    InvalidArguments,
    StaleDesktopState,
    TargetUnavailable,
    UnsupportedDesktopAction,
)
from ..keyboard import KEY_NAMES, parse_hotkey
from ..models import ActionKind, Bounds, DesktopElement, DesktopSnapshot, ExecutableAction

SUPPORTED = frozenset({
    ActionKind.CLICK, ActionKind.DOUBLE_CLICK, ActionKind.RIGHT_CLICK,
    ActionKind.TYPE_TEXT, ActionKind.SET_VALUE, ActionKind.SCROLL,
    ActionKind.PRESS_KEY, ActionKind.HOTKEY, ActionKind.WAIT,
})
_KEY_MAP = {
    "ESCAPE": "ESC", "ARROW_UP": "UP", "ARROW_DOWN": "DOWN", "ARROW_LEFT": "LEFT",
    "ARROW_RIGHT": "RIGHT", "PAGE_UP": "PGUP", "PAGE_DOWN": "PGDN", "SPACE": "SPACE",
    "MINUS": "VK_OEM_MINUS", "EQUAL": "VK_OEM_PLUS", "LEFT_BRACKET": "VK_OEM_4",
    "RIGHT_BRACKET": "VK_OEM_6", "BACKSLASH": "VK_OEM_5", "SEMICOLON": "VK_OEM_1",
    "QUOTE": "VK_OEM_7", "COMMA": "VK_OEM_COMMA", "PERIOD": "VK_OEM_PERIOD",
    "SLASH": "VK_OEM_2", "GRAVE": "VK_OEM_3",
}


def key_sequence(key: str) -> str:
    if key not in KEY_NAMES:
        raise InvalidArguments(f"Unsupported key: {key!r}")
    return "{" + _KEY_MAP.get(key, key) + "}"


def hotkey_sequence(chord: str) -> str:
    modifiers, key = parse_hotkey(chord)
    # MOD is Ctrl on Windows. Deduplicate MOD+CTRL to avoid holding Ctrl twice.
    codes = list(dict.fromkeys({"MOD": "VK_CONTROL", "CTRL": "VK_CONTROL",
                               "ALT": "VK_MENU", "SHIFT": "VK_SHIFT"}[m] for m in modifiers))
    return ("".join("{" + c + " down}" for c in codes) + key_sequence(key)
            + "".join("{" + c + " up}" for c in reversed(codes)))


def literal_sequence(text: str) -> str:
    """User text never becomes pywinauto's keyboard macro language."""
    return "".join("{" + c + "}" if c in "+^%~(){}" else c for c in text)


class WindowsUIAAdapter:
    """Small native boundary that can be substituted in cloud contract tests."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Windows UI Automation needs Windows and an unlocked interactive desktop")
        try:
            from pywinauto import Desktop, keyboard, mouse
        except ImportError as exc:
            raise RuntimeError('Install Windows dependencies: python -m pip install ".[windows]"') from exc
        import ctypes

        self.desktop = Desktop(backend="uia")
        self.keyboard, self.mouse = keyboard, mouse
        self.user32 = ctypes.windll.user32
        # HWND is pointer-sized on 64-bit Windows.
        self.user32.GetForegroundWindow.restype = ctypes.c_void_p
        self.user32.IsWindow.argtypes = [ctypes.c_void_p]
        self.user32.IsIconic.argtypes = [ctypes.c_void_p]
        self.user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]

    def windows(self, pid: int | None = None) -> list[dict[str, Any]]:
        result = []
        for w in self.desktop.windows(visible_only=True):
            info = w.element_info
            if pid is not None and info.process_id != pid:
                continue
            rect = w.rectangle()
            result.append({"pid": info.process_id, "window_id": int(w.handle), "title": info.name,
                           "bounds": Bounds(rect.left, rect.top, rect.width(), rect.height()),
                           "on_screen": w.is_visible(), "minimized": bool(self.user32.IsIconic(w.handle)),
                           "frontmost": int(w.handle) == int(self.user32.GetForegroundWindow() or 0)})
        return result

    def resolve(self, pid: int, window_id: int | None) -> int:
        if window_id is not None:
            self.check_window(pid, window_id)
            return window_id
        windows = [w for w in self.windows(pid) if not w["minimized"]]
        if not windows:
            raise TargetUnavailable(f"PID {pid} has no visible, non-minimized window")
        windows.sort(key=lambda w: not w["frontmost"])
        return windows[0]["window_id"]

    def check_window(self, pid: int, hwnd: int) -> None:
        import ctypes

        owner = ctypes.c_ulong()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if not self.user32.IsWindow(hwnd) or owner.value != pid:
            raise TargetUnavailable("Window closed or handle now belongs to another process")
        if self.user32.IsIconic(hwnd):
            raise TargetUnavailable("Restore the target window before using the Windows MVP")

    def read(self, pid: int, hwnd: int, limit: int) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        self.check_window(pid, hwnd)
        root = self.desktop.window(handle=hwnd).wrapper_object()
        if not root.is_visible():
            raise TargetUnavailable("Target window is hidden")
        rows: list[dict[str, Any]] = []
        pending = [(root, None, 0)]
        seen = set()
        errors = 0
        depth_limited = False
        while pending and len(seen) < limit:
            wrapper, parent, depth = pending.pop()
            try:
                info = wrapper.element_info
                identity = tuple(info.runtime_id)
                if not identity or identity in seen:
                    continue
                seen.add(identity)
                element_id = "uia:" + ":".join(map(str, identity))
                rect = wrapper.rectangle()
                visible = wrapper.is_visible() and rect.width() > 0 and rect.height() > 0
                enabled = wrapper.is_enabled()
                value, writable = None, False
                # Never read a password's Value/Text pattern into observations.
                password = bool(info.element.CurrentIsPassword)
                try:
                    pattern = wrapper.iface_value
                    writable = not bool(pattern.CurrentIsReadOnly)
                    if not password:
                        value = pattern.CurrentValue
                except Exception:
                    pass  # ValuePattern is optional.
                actions = []
                if visible and enabled:
                    actions = [ActionKind.CLICK, ActionKind.DOUBLE_CLICK, ActionKind.RIGHT_CLICK]
                    if writable or (info.control_type == "Edit" and info.element.CurrentIsKeyboardFocusable):
                        actions.append(ActionKind.TYPE_TEXT)
                    if writable:
                        actions.append(ActionKind.SET_VALUE)
                    try:
                        scroll = wrapper.iface_scroll
                        if scroll.CurrentVerticallyScrollable or scroll.CurrentHorizontallyScrollable:
                            actions.append(ActionKind.SCROLL)
                    except Exception:
                        pass
                rows.append({"id": element_id, "role": "TextField" if info.control_type == "Edit"
                             else info.control_type, "name": "" if password else info.name, "value": value,
                             "actions": tuple(actions), "enabled": enabled, "visible": visible,
                             "focused": bool(info.element.CurrentHasKeyboardFocus), "parent_id": parent,
                             "bounds": Bounds(rect.left, rect.top, rect.width(), rect.height()),
                             "metadata": {"automation_id": info.automation_id, "password": password},
                             "ref": wrapper})
                if depth < 40:
                    pending.extend((child, element_id, depth + 1) for child in reversed(wrapper.children()))
                else:
                    depth_limited = True
            except Exception:
                errors += 1  # Inaccessible/disappearing controls are not actionable.
        return root.window_text(), rows, {"truncated": bool(pending) or depth_limited, "read_errors": errors}

    def foreground(self, pid: int, hwnd: int) -> None:
        self.check_window(pid, hwnd)
        if int(self.user32.GetForegroundWindow() or 0) != hwnd:
            self.desktop.window(handle=hwnd).wrapper_object().set_focus()
        self.assert_foreground(hwnd)

    def assert_foreground(self, hwnd: int) -> None:
        if int(self.user32.GetForegroundWindow() or 0) != hwnd:
            raise TargetUnavailable("Windows did not grant foreground focus; activate the target and retry")

    def perform(self, hwnd: int, ref: Any, action: ExecutableAction) -> None:
        self.assert_foreground(hwnd)
        kind = action.kind
        if kind in {ActionKind.CLICK, ActionKind.DOUBLE_CLICK, ActionKind.RIGHT_CLICK}:
            modifier = {None: "", "MOD": "control", "SHIFT": "shift"}[action.click_modifier]
            ref.click_input(button="right" if kind == ActionKind.RIGHT_CLICK else "left",
                            double=kind == ActionKind.DOUBLE_CLICK, pressed=modifier, use_log=False)
        elif kind == ActionKind.SET_VALUE:
            ref.iface_value.SetValue(str(action.value))
        elif kind == ActionKind.TYPE_TEXT:
            ref.set_focus()
            self.assert_foreground(hwnd)
            if not ref.element_info.element.CurrentHasKeyboardFocus:
                raise TargetUnavailable("Editable control did not receive keyboard focus; no text sent")
            # TYPE_TEXT replaces the field, matching upstream text entry semantics.
            self.keyboard.send_keys("^a", pause=0.01)
            self.keyboard.send_keys(literal_sequence(str(action.value)), pause=0.01,
                                    with_spaces=True, with_tabs=True, with_newlines=True)
        elif kind in {ActionKind.HOTKEY, ActionKind.PRESS_KEY}:
            sequence = hotkey_sequence(action.hotkey) if kind == ActionKind.HOTKEY else key_sequence(action.key)
            self.keyboard.send_keys(sequence, pause=0.01)
        elif kind == ActionKind.SCROLL:
            target = ref or self.desktop.window(handle=hwnd).wrapper_object()
            # UIA ScrollPattern: NoAmount=2, SmallDecrement=0, SmallIncrement=3.
            direction = action.scroll_direction
            horizontal = 0 if direction == "LEFT" else 3 if direction == "RIGHT" else 2
            vertical = 0 if direction == "UP" else 3 if direction == "DOWN" else 2
            try:
                target.iface_scroll.Scroll(horizontal, vertical)
            except Exception as exc:
                raise UnsupportedDesktopAction(
                    "Target has no usable UIA ScrollPattern; select a scrollable control"
                ) from exc


class WindowsUIABackend:
    """One exact HWND/PID, with strict snapshot revalidation before native input.

    Foreground input moves the real pointer/focus. UIA calls are synchronous and
    a hung application/provider may block them; the MVP cannot bound COM latency.
    """

    def __init__(self, pid: int, *, window_id: int | None = None, adapter: Any = None,
                 max_elements: int = 1500) -> None:
        if type(pid) is not int or pid <= 0:
            raise ValueError("pid must be a positive integer")
        if window_id is not None and (type(window_id) is not int or window_id <= 0):
            raise ValueError("window_id must be a positive integer")
        if type(max_elements) is not int or max_elements < 1:
            raise ValueError("max_elements must be a positive integer")
        self.pid, self.window_id, self.max_elements = pid, window_id, max_elements
        self.adapter = adapter or WindowsUIAAdapter()
        self._refs: dict[str, Any] = {}

    def open(self) -> None:
        self.window_id = self.adapter.resolve(self.pid, self.window_id)

    def close(self) -> None:
        self._refs.clear()

    def observe(self) -> DesktopSnapshot:
        if self.window_id is None:
            self.open()
        title, rows, diagnostics = self.adapter.read(self.pid, self.window_id, self.max_elements)
        self._refs = {row["id"]: row["ref"] for row in rows}
        elements = tuple(DesktopElement(source="windows_uia", **{k: v for k, v in row.items() if k != "ref"})
                         for row in rows)
        signature = [{**e.compact(), "enabled": e.enabled, "visible": e.visible,
                      "bounds": repr(e.bounds)} for e in elements]
        revision = hashlib.sha256(json.dumps([self.pid, self.window_id, title, signature],
                                            sort_keys=True, default=str).encode()).hexdigest()[:24]
        return DesktopSnapshot(application=f"PID {self.pid}", window=title, revision=revision, elements=elements,
                               context={"pid": self.pid, "window_id": self.window_id,
                                        "backend": "windows_uia", "foreground_required": True, **diagnostics},
                               captured_at_ms=round(time.time() * 1000))

    def is_fresh(self, snapshot: DesktopSnapshot, action: ExecutableAction) -> bool:
        current = self.observe()
        if (snapshot.context.get("pid"), snapshot.context.get("window_id")) != (self.pid, self.window_id):
            return False
        if snapshot.revision != current.revision:
            return False
        if action.target_id:
            try:
                before, after = snapshot.element(action.target_id), current.element(action.target_id)
            except KeyError:
                return False
            return (before.semantic_guard() == after.semantic_guard()
                    and action.target_guard in (None, after.semantic_guard()) and after.enabled and after.visible)
        return True

    def execute(self, snapshot: DesktopSnapshot, action: ExecutableAction) -> None:
        kind = action.kind
        if kind not in SUPPORTED:
            raise UnsupportedDesktopAction(f"Windows MVP does not support {kind.value}")
        if action.click_modifier not in (None, "MOD", "SHIFT"):
            raise InvalidArguments("click_modifier must be MOD or SHIFT")
        if action.click_modifier and kind != ActionKind.CLICK:
            raise InvalidArguments("Only CLICK accepts a modifier")
        target = None
        if action.target_id:
            try:
                target = snapshot.element(action.target_id)
            except KeyError as exc:
                raise ElementNotFound(action.target_id) from exc
            if kind not in target.actions:
                raise ActionNotOffered(f"{kind.value} is not offered by {target.id}")
        if kind in {ActionKind.CLICK, ActionKind.DOUBLE_CLICK, ActionKind.RIGHT_CLICK,
                    ActionKind.TYPE_TEXT, ActionKind.SET_VALUE} and target is None:
            raise InvalidArguments(f"{kind.value} requires a target")
        if kind in {ActionKind.TYPE_TEXT, ActionKind.SET_VALUE} and action.value is None:
            raise InvalidArguments(f"{kind.value} requires a value")
        if kind == ActionKind.PRESS_KEY:
            key_sequence(action.key)
        if kind == ActionKind.HOTKEY:
            hotkey_sequence(action.hotkey)
        if kind == ActionKind.SCROLL and action.scroll_direction not in ("UP", "DOWN", "LEFT", "RIGHT"):
            raise InvalidArguments("scroll_direction must be UP, DOWN, LEFT or RIGHT")
        if not self.is_fresh(snapshot, action):
            raise StaleDesktopState("Windows UI changed; observe again before acting")
        if kind == ActionKind.WAIT:
            time.sleep(0.05)
            return
        self.adapter.foreground(self.pid, self.window_id)
        # Activation can change focus, values or layout. Refuse rather than acting
        # on the old tree; runtime/clients observe again and retry.
        if not self.is_fresh(snapshot, action):
            raise StaleDesktopState("Window changed during activation; observe again")
        ref_id = action.target_id
        if kind == ActionKind.SCROLL and ref_id is None:
            # The existing choice policy emits a direction without a target.
            # Use the focused control's scrollable ancestor, otherwise first visible scroller.
            focused = next((e for e in snapshot.elements if e.focused), None)
            ancestors = set()
            while focused and focused.id not in ancestors:
                ancestors.add(focused.id)
                if ActionKind.SCROLL in focused.actions:
                    ref_id = focused.id
                    break
                focused = snapshot.element(focused.parent_id) if focused.parent_id else None
            if ref_id is None:
                ref_id = next((e.id for e in snapshot.elements if ActionKind.SCROLL in e.actions), None)
            if ref_id is None:
                raise UnsupportedDesktopAction("No visible UIA scrollable control in this window")
        self.adapter.perform(self.window_id, self._refs.get(ref_id), action)
