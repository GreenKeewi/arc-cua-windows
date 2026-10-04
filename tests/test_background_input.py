from types import SimpleNamespace

import pytest

from arc_cua import ActionKind, Decision, DesktopElement, DesktopExecutor, DesktopSnapshot, Subtask
from arc_cua.backends import macos_app, macos_ax, macos_background
from arc_cua.backends.macos_parking import _on_display
from arc_cua.errors import TargetUnavailable
from arc_cua.models import Bounds
from arc_cua.policies import ScriptedPolicy


def test_focus_record_addresses_the_window_and_focus_state():
    record = macos_background.focus_record(0x01020304, True)
    assert len(record) == 0xF8
    assert (record[0x04], record[0x08]) == (0xF8, 0x0D)
    assert record[0x3C:0x40] == bytes([4, 3, 2, 1])
    assert record[0x8A] == 0x01
    assert macos_background.focus_record(1, False)[0x8A] == 0x02


class Item:
    def __init__(self, **attributes):
        self.attributes = attributes


def install_menu(monkeypatch, bar):
    pressed = []
    api = SimpleNamespace(
        AXUIElementCopyAttributeValue=lambda element, name, out: (0, element.attributes.get(name)),
        AXUIElementPerformAction=lambda element, action: pressed.append(element) or 0,
    )
    monkeypatch.setattr(macos_background, "_ax", lambda: api)
    monkeypatch.setattr(macos_ax, "copy_attributes", lambda api, element, names: {
        name: element.attributes.get(name) for name in names
    })
    return Item(AXMenuBar=bar), pressed


def test_menu_shortcut_presses_the_enabled_item_with_matching_modifiers(monkeypatch):
    undo = Item(AXMenuItemCmdChar="Z", AXMenuItemCmdModifiers=0, AXEnabled=True)
    redo = Item(AXMenuItemCmdChar="Z", AXMenuItemCmdModifiers=1, AXEnabled=True)
    bar = Item(AXChildren=[Item(AXChildren=[Item(AXChildren=[undo, redo])])])
    app, pressed = install_menu(monkeypatch, bar)
    assert macos_background.menu_shortcut(app, "z", ("MOD", "SHIFT"))
    assert pressed == [redo]
    assert macos_background.menu_shortcut(app, "z", ("MOD",))
    assert pressed == [redo, undo]


def test_menu_shortcut_skips_disabled_items_and_chords_without_command(monkeypatch):
    save = Item(AXMenuItemCmdChar="S", AXMenuItemCmdModifiers=0, AXEnabled=False)
    app, pressed = install_menu(monkeypatch, Item(AXChildren=[save]))
    assert not macos_background.menu_shortcut(app, "s", ("MOD",))
    assert not macos_background.menu_shortcut(app, "s", ("CTRL",))
    assert pressed == []


def fake_target(monkeypatch, *, select_all=False, menu=False):
    """A MacOSApp for pid 42 whose background input is recorded instead of posted."""
    calls = []
    monkeypatch.setattr(macos_background, "front_pid", lambda: 7)
    monkeypatch.setattr(macos_background, "press", lambda pid, window, code, flags: calls.append(
        ("press", pid, window, code, flags)))
    monkeypatch.setattr(macos_background, "menu_shortcut", lambda app, key, modifiers: calls.append(
        ("menu", key, modifiers)) or menu)
    app = object.__new__(macos_app.MacOSApp)
    app.pid, app.name, app.ax = 42, "Editor", object()
    app._user_pid, app._last_input = None, 0.0
    app._parking, app._made_key = SimpleNamespace(unminimized=None), set()
    app.check_running = lambda: None
    app.key_window = lambda: SimpleNamespace(window_id=9)
    app.select_all = lambda: calls.append(("select_all",)) or select_all
    return app, calls


def test_select_all_shortcut_uses_accessibility_first(monkeypatch):
    app, calls = fake_target(monkeypatch, select_all=True)
    app.shortcut(("MOD",), "A", 0, 1)
    assert calls == [("select_all",)]


def test_command_shortcuts_go_through_the_menu_then_fall_back_to_the_window(monkeypatch):
    app, calls = fake_target(monkeypatch, menu=True)
    app.shortcut(("MOD", "SHIFT"), "COMMA", 43, 3)
    assert calls == [("menu", ",", ("MOD", "SHIFT"))]

    app, calls = fake_target(monkeypatch, menu=False)
    app.shortcut(("MOD",), "S", 1, 1)
    assert calls == [("menu", "s", ("MOD",)), ("press", 42, 9, 1, 1)]


def test_other_chords_are_pressed_in_the_target_window(monkeypatch):
    app, calls = fake_target(monkeypatch)
    app.shortcut(("CTRL",), "K", 40, 8)
    assert calls == [("press", 42, 9, 40, 8)]


def test_click_outside_the_apps_windows_is_refused(monkeypatch):
    app, _ = fake_target(monkeypatch)
    app.windows = lambda: [SimpleNamespace(window_id=9, bounds=Bounds(0, 0, 100, 100))]
    monkeypatch.setattr(macos_background, "click", lambda *args, **kwargs: pytest.fail("clicked"))
    with pytest.raises(Exception, match="outside Editor's visible windows"):
        app.click(Bounds(200, 200, 10, 10))


def test_front_is_handed_back_when_the_app_activates_itself_after_input(monkeypatch):
    app, _ = fake_target(monkeypatch)
    activated = []
    user_app = SimpleNamespace(activateWithOptions_=lambda options: activated.append(options))
    monkeypatch.setitem(__import__("sys").modules, "AppKit", SimpleNamespace(
        NSRunningApplication=SimpleNamespace(runningApplicationWithProcessIdentifier_=lambda pid: user_app)))
    with app.input_scope():
        pass
    assert app._user_pid == 7
    monkeypatch.setattr(macos_background, "front_pid", lambda: 42)
    app.keep_behind()
    assert activated == [0]
    app._last_input -= 10  # Long after the input, switching to the app is the user's choice.
    app.keep_behind()
    assert activated == [0]


def test_parked_window_must_be_on_the_invisible_display():
    display = (1512, 0, 1920, 1080)
    assert _on_display((1552, 40, 586, 488), display)
    assert not _on_display((1210, 56, 586, 488), display)  # Straddles the user's screen.
    assert not _on_display(None, display)


def test_runtime_reports_a_quit_app_instead_of_a_generic_failure():
    class QuitBackend:
        def observe(self):
            element = DesktopElement(id="ok", role="Button", name="OK", actions=(ActionKind.CLICK,))
            return DesktopSnapshot(application="Editor", window="Doc", revision="1", elements=(element,))

        def is_fresh(self, snapshot, action):
            return True

        def execute(self, snapshot, action):
            raise TargetUnavailable("Editor (process ID 42) quit.")

    policy = ScriptedPolicy([Decision(kind=ActionKind.CLICK, target_id="ok")])
    with pytest.raises(TargetUnavailable, match="quit"):
        DesktopExecutor(QuitBackend(), policy).run(Subtask(goal="Press OK", verification=("Done",)))


def test_a_window_brought_back_from_the_dock_is_made_key_once_before_keys(monkeypatch):
    app, calls = fake_target(monkeypatch)
    monkeypatch.setattr(macos_background, "window_id", lambda ref: 9)
    monkeypatch.setattr(macos_background, "click", lambda pid, window, point: calls.append(("click", window, point)))
    monkeypatch.setattr(macos_app, "_inert_point", lambda ax, bounds: (50.0, 5.0))
    app.window = lambda window_id: SimpleNamespace(window_id=window_id, bounds=Bounds(0, 0, 100, 100))
    app._parking = SimpleNamespace(unminimized="restored window")
    app.press(36)
    app.press(36)
    assert calls == [("click", 9, (50.0, 5.0)), ("press", 42, 9, 36, 0), ("press", 42, 9, 36, 0)]
