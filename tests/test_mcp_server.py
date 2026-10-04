from __future__ import annotations

import base64
import io
import json

from test_driver import PID, A, B, FakeApp

from arc_cua.driver import Driver, WindowTarget
from arc_cua.mcp_server import Server


def server() -> Server:
    return Server(Driver(app_factory=FakeApp))


def call(srv: Server, name: str, **arguments):
    params = {"name": name, "arguments": arguments}
    reply = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})
    return reply["result"]


def test_initialize_lists_tools_and_ignores_notifications():
    srv = server()
    init = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["capabilities"] == {"tools": {}}
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert {t["name"] for t in tools} >= {"observe", "act", "wait", "commands", "run_command"}


def test_observe_then_act_by_snapshot_and_element():
    srv = server()
    observed = call(srv, "observe", pid=PID)["structuredContent"]
    assert observed["window_id"] == A
    assert observed["elements"] == [{"id": "a_submit", "role": "Button", "name": "Submit", "actions": ["CLICK"]}]
    acted = call(srv, "act", snapshot=observed["snapshot"], action="CLICK", element="a_submit")["structuredContent"]
    assert acted["status"] == "done"


def test_act_returns_a_fresh_snapshot_when_the_app_changed():
    srv = server()
    observed = call(srv, "observe", pid=PID)["structuredContent"]
    app = srv.driver._app(PID)
    app.desktop.sheet = True
    app.journal.add("AXSheetCreated")
    acted = call(srv, "act", snapshot=observed["snapshot"], action="CLICK", element="a_submit")["structuredContent"]
    assert acted["status"] == "changed"
    assert acted["fresh"]["snapshot"] != observed["snapshot"]
    assert acted["fresh"]["window_id"] == A
    assert app.desktop.executed == []


def test_tool_errors_are_reported_not_raised():
    srv = server()
    result = call(srv, "act", snapshot="s404", action="CLICK", element="a_submit")
    assert result["isError"] and "observe again" in result["content"][0]["text"]
    observed = call(srv, "observe", pid=PID)["structuredContent"]
    result = call(srv, "act", snapshot=observed["snapshot"], action="SET_VALUE", element="a_submit", value="x")
    assert result["isError"]
    assert call(srv, "nope")["isError"]


def test_serve_answers_line_by_line():
    srv = server()
    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"
        + "not json\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
    )
    stdout = io.StringIO()
    srv.serve(stdin, stdout)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert replies[0] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert replies[1]["error"]["code"] == -32700
    assert len(replies) == 2


class RawDriver:
    """Stands in for Driver's pixel methods."""

    def __init__(self) -> None:
        self.calls = []

    def screenshot(self, where, snapshot=None):
        from arc_cua.driver import Screenshot

        return Screenshot(png=b"\x89PNG fake", width=400, height=300, scale=2.0, window_id=7, title="Canvas")

    def click_at(self, where, x, y, **options):
        from arc_cua.driver import ActResult

        self.calls.append(("click_at", where, x, y, options))
        return ActResult("done", None, (), 1.0)

    def parked(self, pid):
        return False

    def close(self):
        pass


def test_screenshot_is_sent_as_image_content():
    srv = Server(RawDriver())
    result = call(srv, "screenshot", pid=PID)
    assert result["structuredContent"]["screenshot"]["scale"] == 2.0
    assert "_png" not in result["structuredContent"]
    image = result["content"][1]
    assert image["type"] == "image" and image["mimeType"] == "image/png"
    assert base64.b64decode(image["data"]) == b"\x89PNG fake"


def test_click_at_passes_points_and_options():
    driver = RawDriver()
    srv = Server(driver)
    result = call(srv, "click_at", pid=PID, x=12.5, y=40, button="right", modifiers=["SHIFT"])
    assert result["structuredContent"]["status"] == "done"
    assert driver.calls == [("click_at", PID, 12.5, 40, {
        "button": "right", "count": 1, "modifiers": ("SHIFT",), "snapshot": None,
    })]
    call(srv, "click_at", pid=PID, x=1, y=2, window_id=7)
    assert driver.calls[-1][1] == WindowTarget(PID, 7)


def test_raw_tools_are_listed():
    tools = Server(RawDriver()).handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert {"screenshot", "click_at", "drag", "scroll_at", "press", "type_text"} <= {t["name"] for t in tools}


def test_observe_takes_an_exact_window_and_raw_input_defaults_to_the_snapshot_window():
    srv = server()
    observed = call(srv, "observe", pid=PID, window_id=B)["structuredContent"]
    assert observed["window_id"] == B and observed["window"] == "Form B"
    app = srv.driver._app(PID)
    call(srv, "type_text", pid=PID, text="hi", snapshot=observed["snapshot"])
    assert app.app.inputs == [("type", B, "hi")]


def test_release_expires_the_apps_snapshots_and_puts_it_back():
    srv = server()
    observed = call(srv, "observe", pid=PID)["structuredContent"]
    app = srv.driver._app(PID)
    assert call(srv, "release", pid=PID)["structuredContent"] == {"released": [PID]}
    assert app.closed
    result = call(srv, "act", snapshot=observed["snapshot"], action="CLICK", element="a_submit")
    assert result["isError"] and "observe again" in result["content"][0]["text"]
    assert call(srv, "release", pid=PID)["structuredContent"] == {"released": []}


def test_release_without_a_pid_releases_every_app():
    srv = server()
    call(srv, "observe", pid=PID)
    srv.driver._app(PID + 1)
    assert call(srv, "release")["structuredContent"] == {"released": [PID, PID + 1]}
    assert srv._snapshots == {}


def test_results_say_when_a_window_was_parked():
    srv = server()
    observed = call(srv, "observe", pid=PID)["structuredContent"]
    acted = call(srv, "act", snapshot=observed["snapshot"], action="CLICK", element="a_submit")["structuredContent"]
    assert "parked" not in acted
    srv.driver._app(PID).app.parked.append(A)  # As when input needed a minimized window on a display.
    typed = call(srv, "type_text", pid=PID, text="hi", snapshot=observed["snapshot"])["structuredContent"]
    assert typed["parked"] is True
