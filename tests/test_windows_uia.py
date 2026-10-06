"""Cloud contract tests use a fake native boundary; they prove no live desktop behavior."""
from dataclasses import replace

import pytest

from arc_cua.backends.windows_uia import (
    WindowsUIABackend,
    hotkey_sequence,
    key_sequence,
    literal_sequence,
)
from arc_cua.errors import ActionNotOffered, InvalidArguments, StaleDesktopState, UnsupportedDesktopAction
from arc_cua.models import ActionKind as K
from arc_cua.models import Bounds, Decision, ExecutableAction, Subtask, TerminalKind
from arc_cua.runtime import DesktopExecutor, RuntimeConfig
from arc_cua.windows_driver import WindowsDriver


class FakeUIA:
    def __init__(self):
        self.rows = [{"id": "uia:1:2", "role": "TextField", "name": "Message", "value": "",
                      "actions": (K.TYPE_TEXT, K.SET_VALUE, K.CLICK, K.SCROLL),
                      "enabled": True, "visible": True, "focused": True, "parent_id": None,
                      "bounds": Bounds(1, 2, 100, 20), "metadata": {}, "ref": object()}]
        self.calls = []
        self.on_activate = None

    def resolve(self, pid, hwnd):
        return hwnd or 100

    def read(self, pid, hwnd, limit):
        return "Fixture", self.rows, {"truncated": False, "read_errors": 0}

    def foreground(self, pid, hwnd):
        self.calls.append(("foreground", hwnd))
        if self.on_activate:
            self.on_activate()

    def perform(self, hwnd, ref, action):
        self.calls.append(("perform", action.kind, ref))
        if action.kind in (K.TYPE_TEXT, K.SET_VALUE):
            self.rows[0]["value"] = action.value

    def windows(self, pid=None):
        return [{"pid": 42, "window_id": 100, "title": "Fixture", "frontmost": True,
                 "bounds": Bounds(0, 0, 200, 200), "on_screen": True, "minimized": False}]


def make():
    adapter = FakeUIA()
    backend = WindowsUIABackend(42, adapter=adapter)
    return backend, adapter, backend.observe()


def test_observes_named_control_and_exact_window():
    backend, _, snapshot = make()
    assert snapshot.context["window_id"] == 100
    assert snapshot.element("uia:1:2").name == "Message"
    assert snapshot.elements[0].source == "windows_uia"
    assert backend.observe().revision == snapshot.revision


@pytest.mark.parametrize("field,value", [
    ("name", "Changed"), ("value", "new"), ("enabled", False), ("visible", False),
    ("bounds", Bounds(9, 9, 20, 20)), ("focused", False), ("id", "uia:9:9"),
])
def test_stale_state_never_sends_input(field, value):
    backend, adapter, snapshot = make()
    adapter.rows[0][field] = value
    with pytest.raises(StaleDesktopState):
        backend.execute(snapshot, ExecutableAction(K.CLICK, target_id="uia:1:2"))
    assert adapter.calls == []


def test_activation_change_is_checked_before_input():
    backend, adapter, snapshot = make()
    adapter.on_activate = lambda: adapter.rows[0].update(name="replaced")
    with pytest.raises(StaleDesktopState):
        backend.execute(snapshot, ExecutableAction(K.CLICK, target_id="uia:1:2"))
    assert adapter.calls == [("foreground", 100)]


def test_wrong_window_and_guard_rejected():
    backend, adapter, snapshot = make()
    other = replace(snapshot, context={"pid": 42, "window_id": 200})
    assert not backend.is_fresh(other, ExecutableAction(K.HOTKEY, hotkey="MOD+A"))
    assert not backend.is_fresh(snapshot, ExecutableAction(K.CLICK, target_id="uia:1:2", target_guard="wrong"))
    assert not adapter.calls


@pytest.mark.parametrize("action", [
    ExecutableAction(K.CLICK, target_id="uia:1:2"),
    ExecutableAction(K.TYPE_TEXT, target_id="uia:1:2", value="literal + {ENTER}"),
    ExecutableAction(K.SET_VALUE, target_id="uia:1:2", value="abc"),
    ExecutableAction(K.SCROLL, target_id="uia:1:2", scroll_direction="DOWN"),
    ExecutableAction(K.PRESS_KEY, key="ENTER"), ExecutableAction(K.HOTKEY, hotkey="MOD+SHIFT+S"),
])
def test_supported_actions_reach_native_boundary(action):
    backend, adapter, snapshot = make()
    backend.execute(snapshot, action)
    assert adapter.calls[0] == ("foreground", 100)
    assert adapter.calls[-1][0:2] == ("perform", action.kind)


@pytest.mark.parametrize("action,error", [
    (ExecutableAction(K.DRAG_BY, target_id="uia:1:2"), UnsupportedDesktopAction),
    (ExecutableAction(K.DOUBLE_CLICK, target_id="uia:1:2"), ActionNotOffered),
    (ExecutableAction(K.TYPE_TEXT, target_id="uia:1:2"), InvalidArguments),
    (ExecutableAction(K.SCROLL, scroll_direction="diagonal"), InvalidArguments),
    (ExecutableAction(K.PRESS_KEY, key="{ENTER}"), InvalidArguments),
    (ExecutableAction(K.CLICK), InvalidArguments),
])
def test_invalid_actions_never_activate(action, error):
    backend, adapter, snapshot = make()
    with pytest.raises(error):
        backend.execute(snapshot, action)
    assert not adapter.calls


def test_keyboard_macro_escaping_and_ctrl_mapping():
    assert hotkey_sequence("MOD+A") == "{VK_CONTROL down}{A}{VK_CONTROL up}"
    assert key_sequence("ARROW_DOWN") == "{DOWN}"
    assert literal_sequence("+{ENTER} café") == "{+}{{}ENTER{}} café"
    with pytest.raises(ValueError):
        hotkey_sequence("CTRL+A; DELETE")


def test_existing_decision_loop_executes_windows_backend():
    backend, _, _ = make()

    class Policy:
        def decide(self, *, subtask, snapshot, history):
            if not history:
                return Decision(kind=K.TYPE_TEXT, target_id=snapshot.elements[0].id, input_key="message")
            return Decision(terminal=TerminalKind.SUBTASK_COMPLETE)

    result = DesktopExecutor(backend, Policy(), config=RuntimeConfig(
        verify=lambda snapshot, task: snapshot.elements[0].value == "Hello")).run(
            Subtask(goal="fill", verification=("contains Hello",), inputs={"message": "Hello"}))
    assert result.status == TerminalKind.SUBTASK_COMPLETE
    assert result.actions_taken == 1


def test_windows_mcp_tools_and_stale_refresh():
    from arc_cua.mcp_server import Server

    adapter = FakeUIA()
    server = Server(WindowsDriver(adapter=adapter))
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert {t["name"] for t in listed} == {"status", "apps", "windows", "observe", "act", "settle", "wait", "release"}
    init = server.handle({"id": 2, "method": "initialize"})
    assert "MOD means Ctrl" in init["result"]["instructions"]
    observed = server.call("observe", {"pid": 42})
    adapter.rows[0]["name"] = "different"
    result = server.call("act", {"snapshot": observed["snapshot"], "action": "CLICK", "element": "uia:1:2"})
    assert result["status"] == "stale" and result["fresh"]["snapshot"] != observed["snapshot"]
    assert not adapter.calls
    denied = server._tool_result("click_at", {"pid": 42, "x": 1, "y": 2})
    assert denied["isError"] and denied["structuredContent"]["code"] == "unknown_tool"
    assert server.call("release", {"pid": 42}) == {"released": [42]}
    server.close()


def test_cli_windows_factory_and_platform_default(monkeypatch):
    from arc_cua import cli

    monkeypatch.setattr(cli.sys, "platform", "win32")
    request = cli.parse_request({"app": {"pid": 42}, "subtask": {"goal": "read", "verification": ["read"]},
                                 "provider": {"name": "jev"}})
    assert request.backend == "windows"
    monkeypatch.setattr("arc_cua.backends.windows_uia.WindowsUIAAdapter", FakeUIA)
    assert isinstance(cli.make_backend({"pid": 42}, "windows"), WindowsUIABackend)
    with pytest.raises(ValueError, match="app.pid"):
        cli.make_backend({"bundle_id": "com.example"}, "windows")


def test_windows_mcp_cli_has_no_sighup_dependency(monkeypatch):
    from arc_cua import cli, mcp_server

    monkeypatch.delattr(cli.signal, "SIGHUP", raising=False)
    monkeypatch.setattr(mcp_server, "serve", lambda stdin, stdout: 0)
    assert cli.main(["mcp"]) == 0


def test_scroll_target_survives_action_validation():
    from arc_cua.validation import materialize_action

    _, _, snapshot = make()
    action = materialize_action(Decision(kind=K.SCROLL, target_id="uia:1:2", scroll_direction="DOWN"),
                                snapshot, Subtask(goal="scroll", verification=("list moved",)))
    assert action.target_id == "uia:1:2" and action.target_guard == snapshot.elements[0].semantic_guard()


def test_global_scroll_uses_visible_scrollable_control():
    backend, adapter, snapshot = make()
    backend.execute(snapshot, ExecutableAction(K.SCROLL, scroll_direction="DOWN"))
    assert adapter.calls[-1][2] is adapter.rows[0]["ref"]


def native_fake():
    from types import SimpleNamespace as NS

    from arc_cua.backends.windows_uia import WindowsUIAAdapter

    class Wrapper:
        def __init__(self, identity, *, password=False):
            self.element_info = NS(runtime_id=(identity,), process_id=42, name="secret" if password else "Message",
                                   control_type="Edit", automation_id="field",
                                   element=NS(CurrentIsPassword=password, CurrentHasKeyboardFocus=True,
                                              CurrentIsKeyboardFocusable=True))
            self.iface_value = NS(CurrentIsReadOnly=False, CurrentValue="sensitive", SetValue=lambda value: None)
            self.iface_scroll = NS(CurrentVerticallyScrollable=True, CurrentHorizontallyScrollable=False)
            self.child_nodes = []
            self.clicked = None

        def is_visible(self):
            return True

        def is_enabled(self):
            return True

        def rectangle(self):
            return NS(left=10, top=20, width=lambda: 100, height=lambda: 30)

        def children(self):
            return self.child_nodes

        def window_text(self):
            return "Fixture"

        def set_focus(self):
            pass

        def click_input(self, **kwargs):
            self.clicked = kwargs

    root, password = Wrapper(1), Wrapper(2, password=True)
    root.child_nodes = [password]
    adapter = object.__new__(WindowsUIAAdapter)
    adapter.check_window = lambda pid, hwnd: None
    adapter.assert_foreground = lambda hwnd: None
    adapter.desktop = NS(window=lambda **kwargs: NS(wrapper_object=lambda: root))
    adapter.user32 = NS(GetForegroundWindow=lambda: 100)
    sent = []
    adapter.keyboard = NS(send_keys=lambda sequence, **kwargs: sent.append(sequence))
    return adapter, root, password, sent


def test_native_observation_hides_password_and_reports_truncation():
    adapter, root, password, _ = native_fake()
    _, rows, diagnostics = adapter.read(42, 100, 10)
    assert rows[0]["name"] == "Message" and rows[0]["value"] == "sensitive"
    assert rows[1]["name"] == "" and rows[1]["value"] is None
    assert rows[1]["metadata"]["password"]
    assert K.TYPE_TEXT in rows[1]["actions"]
    assert not diagnostics["truncated"]
    _, limited, diagnostics = adapter.read(42, 100, 1)
    assert len(limited) == 1 and diagnostics["truncated"]


def test_native_modifier_and_literal_input_mapping():
    adapter, root, _, sent = native_fake()
    adapter.perform(100, root, ExecutableAction(K.CLICK, click_modifier="MOD"))
    assert root.clicked["pressed"] == "control"
    assert not root.clicked["use_log"]
    adapter.perform(100, root, ExecutableAction(K.TYPE_TEXT, value="+{ENTER} café"))
    assert sent == ["^a", "{+}{{}ENTER{}} café"]
    root.element_info.element.CurrentHasKeyboardFocus = False
    with pytest.raises(Exception, match="keyboard focus"):
        adapter.perform(100, root, ExecutableAction(K.TYPE_TEXT, value="should not type"))
    assert len(sent) == 2


def test_native_foreground_does_not_reset_existing_focus():
    adapter, root, _, _ = native_fake()
    root.set_focus = lambda: pytest.fail("Already foreground: don't reset control focus")
    adapter.foreground(42, 100)


def test_native_scroll_uses_uia_enumeration():
    adapter, root, _, _ = native_fake()
    calls = []
    root.iface_scroll.Scroll = lambda horizontal, vertical: calls.append((horizontal, vertical))
    adapter.perform(100, root, ExecutableAction(K.SCROLL, scroll_direction="DOWN"))
    adapter.perform(100, root, ExecutableAction(K.SCROLL, scroll_direction="LEFT"))
    assert calls == [(2, 3), (0, 2)]


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Native imports require Windows")
def test_windows_native_dependency_imports():
    from arc_cua.backends.windows_uia import WindowsUIAAdapter

    assert WindowsUIAAdapter().desktop.backend.name == "uia"
