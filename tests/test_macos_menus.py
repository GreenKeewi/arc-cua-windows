import pytest

from arc_cua.backends.macos_menus import MenuCommand, _shortcut
from arc_cua.keyboard import parse_hotkey


@pytest.mark.parametrize(("char", "modifiers", "expected"), [
    ("S", 0, "MOD+S"),
    ("s", 1, "MOD+SHIFT+S"),
    (",", 0, "MOD+COMMA"),
    ("\x08", 1, "MOD+SHIFT+BACKSPACE"),
    ("", 2, "MOD+ALT+ARROW_LEFT"),
    ("", 8, "F2"),
    ("N", 4, "MOD+CTRL+N"),
    (None, 0, None),
    ("", 0, None),
])
def test_menu_shortcuts_use_arc_chord_names(char, modifiers, expected):
    assert _shortcut(char, modifiers) == expected


def test_menu_shortcuts_with_modifiers_parse_as_hotkeys():
    for char, modifiers in (("S", 1), ("\x08", 0), ("", 6), (".", 0)):
        parse_hotkey(_shortcut(char, modifiers))


def test_compact_leaves_out_defaults():
    command = MenuCommand(("File", "Save"), "MOD+S", enabled=True, checked=False)
    assert command.compact() == {"path": "File > Save", "shortcut": "MOD+S"}
    disabled = MenuCommand(("View", "Sidebar"), None, enabled=False, checked=True)
    assert disabled.compact() == {"path": "View > Sidebar", "enabled": False, "checked": True}
