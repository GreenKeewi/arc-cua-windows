"""An app's menu bar as a list of commands it can run.

The menu bar names almost everything a Mac app can do, with its shortcut and
whether it is available right now. Accessibility exposes the whole bar without
opening a menu, and pressing an item runs it while the app stays in the
background. A full bar reads in tens of milliseconds.

The Apple menu belongs to the system, not the app, and is left out by default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import TargetUnavailable, UnsupportedDesktopAction

_ATTRIBUTES = (
    "AXRole", "AXTitle", "AXEnabled", "AXMenuItemCmdChar", "AXMenuItemCmdModifiers", "AXMenuItemMarkChar",
    "AXChildren",
)
# AXMenuItemCmdModifiers bits; Command is implied unless _NO_COMMAND is set.
_SHIFT, _OPTION, _CONTROL, _NO_COMMAND = 1, 2, 4, 8
_KEY_NAMES = {
    ",": "COMMA", ".": "PERIOD", "/": "SLASH", ";": "SEMICOLON", "'": "QUOTE", "[": "LEFT_BRACKET",
    "]": "RIGHT_BRACKET", "\\": "BACKSLASH", "-": "MINUS", "=": "EQUAL", "`": "GRAVE", " ": "SPACE",
    # Menus store special keys as control characters or AppKit's private-use function keys.
    "\x08": "BACKSPACE", "\x7f": "BACKSPACE", "\r": "ENTER", "\x03": "ENTER", "\t": "TAB", "\x1b": "ESCAPE",
    "": "ARROW_UP", "": "ARROW_DOWN", "": "ARROW_LEFT", "": "ARROW_RIGHT",
    "": "DELETE", "": "HOME", "": "END", "": "PAGE_UP", "": "PAGE_DOWN",
    **{chr(0xF704 + index): f"F{index + 1}" for index in range(20)},
}
_MAX_DEPTH = 6


@dataclass(frozen=True, slots=True)
class MenuCommand:
    path: tuple[str, ...]  # ("File", "Export", "PDF…")
    shortcut: str | None  # In arc-cua chord syntax, such as "MOD+SHIFT+S".
    enabled: bool
    checked: bool

    def compact(self) -> dict[str, Any]:
        data: dict[str, Any] = {"path": " > ".join(self.path)}
        if self.shortcut:
            data["shortcut"] = self.shortcut
        if not self.enabled:
            data["enabled"] = False
        if self.checked:
            data["checked"] = True
        return data


def read_commands(pid: int, *, query: str | None = None, include_apple_menu: bool = False) -> list[MenuCommand]:
    """The app's menu commands in menu order; ``query`` keeps those whose path contains it."""
    commands: list[MenuCommand] = []
    for path, attributes, _ in _items(pid, include_apple_menu=include_apple_menu):
        commands.append(MenuCommand(
            path=path,
            shortcut=_shortcut(attributes.get("AXMenuItemCmdChar"), attributes.get("AXMenuItemCmdModifiers")),
            enabled=attributes.get("AXEnabled") is True,
            checked=bool(attributes.get("AXMenuItemMarkChar")),
        ))
    if query:
        needle = query.casefold()
        commands = [c for c in commands if needle in " > ".join(c.path).casefold()]
    return commands


def run_command(pid: int, path: tuple[str, ...] | list[str] | str) -> None:
    """Run the menu command at ``path`` (a tuple, or a string joined with " > "),
    found in the menu as it is now. Raises UnsupportedDesktopAction when there is no
    such command or it is not available."""
    if isinstance(path, str):
        path = tuple(part.strip() for part in path.split(">"))
    wanted = tuple(path)
    for found, attributes, element in _items(pid, include_apple_menu=True):
        if found != wanted:
            continue
        if attributes.get("AXEnabled") is not True:
            raise UnsupportedDesktopAction(f"Menu command {' > '.join(wanted)!r} is not available right now")
        import ApplicationServices as AS  # type: ignore

        error = AS.AXUIElementPerformAction(element, "AXPress")
        if error != 0:
            raise UnsupportedDesktopAction(f"Pressing menu command {' > '.join(wanted)!r} failed with error {error}")
        return
    raise UnsupportedDesktopAction(f"No menu command {' > '.join(wanted)!r}")


def _items(pid: int, *, include_apple_menu: bool):
    """Yield (path, attributes, element) for each runnable menu item, in menu order."""
    import ApplicationServices as AS  # type: ignore

    from .macos_ax import copy_attributes

    app = AS.AXUIElementCreateApplication(pid)
    error, bar = AS.AXUIElementCopyAttributeValue(app, "AXMenuBar", None)
    if error != 0 or bar is None:
        raise TargetUnavailable(f"The app with process ID {pid} exposes no menu bar")

    def read(element: Any) -> dict[str, Any]:
        attributes = copy_attributes(AS, element, _ATTRIBUTES)
        if attributes is None:  # Batch reads unsupported: one call per attribute.
            attributes = {}
            for name in _ATTRIBUTES:
                status, value = AS.AXUIElementCopyAttributeValue(element, name, None)
                attributes[name] = value if status == 0 else None
        return attributes

    def walk(element: Any, path: tuple[str, ...], depth: int):
        for child in read(element).get("AXChildren") or ():
            attributes = read(child)
            role = attributes.get("AXRole")
            if role == "AXMenu":
                if depth < _MAX_DEPTH:
                    yield from walk(child, path, depth + 1)
                continue
            if role not in ("AXMenuBarItem", "AXMenuItem"):
                continue
            title = str(attributes.get("AXTitle") or "")
            if not title:  # A separator.
                continue
            if attributes.get("AXChildren"):
                if depth < _MAX_DEPTH:
                    yield from walk(child, (*path, title), depth + 1)
            else:
                yield (*path, title), attributes, child

    top = list(read(bar).get("AXChildren") or ())
    if not include_apple_menu:
        top = top[1:]  # The first menu bar item is always the Apple menu.
    for item in top:
        attributes = read(item)
        title = str(attributes.get("AXTitle") or "")
        if title:
            yield from walk(item, (title,), 1)


def _shortcut(char: Any, modifiers: Any) -> str | None:
    if not isinstance(char, str) or not char:
        return None
    bits = modifiers if isinstance(modifiers, int) else 0
    parts = [name for bit, name in ((_CONTROL, "CTRL"), (_OPTION, "ALT"), (_SHIFT, "SHIFT")) if bits & bit]
    if not bits & _NO_COMMAND:
        parts.insert(0, "MOD")
    key = _KEY_NAMES.get(char, char.upper())
    return "+".join([*parts, key]) if parts else key
