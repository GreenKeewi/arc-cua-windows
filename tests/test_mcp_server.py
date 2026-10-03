from __future__ import annotations

import io
import json

from test_driver import PID, FakeApp

from arc_cua.driver import Driver
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
    assert observed["elements"] == [{"id": "submit", "role": "Button", "name": "Submit", "actions": ["CLICK"]}]
    acted = call(srv, "act", snapshot=observed["snapshot"], action="CLICK", element="submit")["structuredContent"]
    assert acted["status"] == "done"


def test_act_returns_a_fresh_snapshot_when_the_app_changed():
    srv = server()
    observed = call(srv, "observe", pid=PID)["structuredContent"]
    app = srv.driver._app(PID)
    app.backend.sheet = True
    app.journal.add("AXSheetCreated")
    acted = call(srv, "act", snapshot=observed["snapshot"], action="CLICK", element="submit")["structuredContent"]
    assert acted["status"] == "changed"
    assert acted["fresh"]["snapshot"] != observed["snapshot"]
    assert app.backend.executed == []


def test_tool_errors_are_reported_not_raised():
    srv = server()
    result = call(srv, "act", snapshot="s404", action="CLICK", element="submit")
    assert result["isError"] and "observe again" in result["content"][0]["text"]
    observed = call(srv, "observe", pid=PID)["structuredContent"]
    result = call(srv, "act", snapshot=observed["snapshot"], action="SET_VALUE", element="submit", value="x")
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
