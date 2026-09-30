import io
import json
import subprocess
import sys

import pytest

from arc_cua import ActionKind, Decision, DesktopElement, DesktopSnapshot, TerminalKind, cli
from arc_cua.backends import StateMachineBackend
from arc_cua.errors import TargetUnavailable
from arc_cua.policies import ScriptedPolicy

REQUEST = {
    "app": {"pid": 4242},
    "subtask": {
        "goal": "Search for Zurich",
        "inputs": {"city": "Zurich"},
        "verification": ["The search field contains Zurich"],
    },
    "provider": {"name": "jev", "api_key": "secret-key"},
}


def snapshot(state):
    field = DesktopElement(id="search", role="TextField", name="Search", value=state["value"],
                           actions=(ActionKind.TYPE_TEXT,))
    return DesktopSnapshot(application="Maps", window="Main", revision=state["value"], elements=(field,))


def transition(state, action):
    state["value"] = action.value


def run(monkeypatch, request, *, decisions=(), backend=None):
    made = {}

    def make_policy(api_key, model):
        made["api_key"] = api_key
        return ScriptedPolicy(decisions)

    def make_backend(app, kind):
        made["app"], made["backend"] = app, kind
        if isinstance(backend, Exception):
            raise backend
        return backend or StateMachineBackend({"value": ""}, snapshot, transition)

    monkeypatch.setitem(cli.PROVIDERS, "jev", make_policy)
    monkeypatch.setattr(cli, "make_backend", make_backend)
    out = io.StringIO()
    code = cli.run(io.StringIO(json.dumps(request)), out)
    return code, [json.loads(line) for line in out.getvalue().splitlines()], made


def test_run_prints_one_line_per_action_then_the_result(monkeypatch):
    code, lines, made = run(monkeypatch, REQUEST, decisions=[
        Decision(kind=ActionKind.TYPE_TEXT, target_id="search", input_key="city", confidence=0.9),
        Decision(terminal=TerminalKind.SUBTASK_COMPLETE),
    ])
    assert code == 0
    assert [line["type"] for line in lines] == ["action", "result"]
    assert lines[0]["action"] == "TYPE_TEXT"
    assert lines[0]["value"] == "Zurich"
    assert lines[0]["confidence"] == 0.9
    assert lines[1]["status"] == "SUBTASK_COMPLETE"
    assert lines[1]["actions_taken"] == 1
    assert made == {"api_key": "secret-key", "app": {"pid": 4242}, "backend": "hybrid"}


def test_backend_is_opened_and_closed_around_the_run(monkeypatch):
    events = []
    backend = StateMachineBackend({"value": ""}, snapshot, transition)
    backend.open = lambda: events.append("open")
    backend.close = lambda: events.append("close")
    code, lines, _ = run(monkeypatch, REQUEST, backend=backend,
                         decisions=[Decision(terminal=TerminalKind.BLOCKED)])
    assert code == 0 and lines[-1]["status"] == "BLOCKED"
    assert events == ["open", "close"]


def test_unavailable_app_ends_with_an_error_line(monkeypatch):
    code, lines, _ = run(monkeypatch, REQUEST, backend=TargetUnavailable("No app is running with process ID 4242."))
    assert code == 1
    assert lines == [{"type": "error", "error": "No app is running with process ID 4242."}]


@pytest.mark.parametrize(("change", "message"), [
    ({"app": {"name": "Maps"}}, "app must be"),
    ({"app": {"pid": True}}, "app.pid"),
    ({"provider": {"name": "other"}}, "provider.name"),
    ({"provider": {"name": "jev", "key": "x"}}, "Unknown provider fields"),
    ({"subtask": {"goal": "Search"}}, "subtask: Missing required subtask fields"),
    ({"backend": "chrome"}, "backend must be"),
    ({"timeout_s": 0}, "timeout_s"),
    ({"min_confidence": 2}, "min_confidence"),
    ({"extra": 1}, "Unknown fields"),
])
def test_invalid_requests_are_rejected_before_anything_runs(monkeypatch, change, message):
    code, lines, made = run(monkeypatch, {**REQUEST, **change})
    assert code == 2
    assert len(lines) == 1 and lines[0]["type"] == "error"
    assert message in lines[0]["error"]
    assert "app" not in made


def test_runtime_options_reach_the_executor():
    request = cli.parse_request({**REQUEST, "backend": "ax", "timeout_s": 30, "min_margin": 0.1})
    assert request.backend == "ax"
    assert (request.config.timeout_s, request.config.min_margin) == (30, 0.1)


def test_command_keeps_logs_off_standard_output():
    completed = subprocess.run(
        [sys.executable, "-m", "arc_cua", "run"], input="not json", capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 2
    assert [json.loads(line)["type"] for line in completed.stdout.splitlines()] == ["error"]
    assert "not valid JSON" in completed.stderr


def test_needs_input_is_a_result_naming_the_field(monkeypatch):
    code, lines, _ = run(monkeypatch, REQUEST, decisions=[
        Decision(terminal=TerminalKind.NEEDS_INPUT, target_id="search", reason="needs text"),
    ])
    assert code == 0
    assert lines[-1]["status"] == "NEEDS_INPUT"
    assert lines[-1]["needs_input"] == {"element_id": "search", "role": "TextField", "name": "Search", "value": ""}


def test_secret_inputs_are_redacted_in_every_line(monkeypatch):
    request = {**REQUEST, "subtask": {**REQUEST["subtask"], "secret_inputs": ["city"]}}
    code, lines, _ = run(monkeypatch, request, decisions=[
        Decision(kind=ActionKind.TYPE_TEXT, target_id="search", input_key="city"),
        Decision(terminal=TerminalKind.SUBTASK_COMPLETE),
    ])
    assert code == 0
    assert "Zurich" not in json.dumps(lines)
    assert lines[0]["value"] == "[secret]"


def test_log_file_records_each_decision(monkeypatch):
    log = io.StringIO()
    policy = ScriptedPolicy([
        Decision(kind=ActionKind.TYPE_TEXT, target_id="search", input_key="city", confidence=0.8, latency_ms=12),
        Decision(terminal=TerminalKind.SUBTASK_COMPLETE, confidence=0.9),
    ])
    backend = StateMachineBackend({"value": ""}, snapshot, transition)
    monkeypatch.setitem(cli.PROVIDERS, "jev", lambda api_key, model: policy)
    monkeypatch.setattr(cli, "make_backend", lambda app, kind: backend)
    code = cli.run(io.StringIO(json.dumps(REQUEST)), io.StringIO(), log)
    entries = [json.loads(line) for line in log.getvalue().splitlines()]
    assert code == 0
    assert [(e["step"], e["choice"], e["outcome"]) for e in entries] == [
        (1, "TYPE_TEXT", None), (2, "SUBTASK_COMPLETE", "SUBTASK_COMPLETE"),
    ]
    assert entries[0]["decide_ms"] == 12 and entries[0]["state_changed"] is True


def test_dry_run_request_plans_without_acting(monkeypatch):
    code, lines, _ = run(monkeypatch, {**REQUEST, "dry_run": True}, decisions=[
        Decision(kind=ActionKind.TYPE_TEXT, target_id="search", input_key="city"),
    ])
    assert code == 0
    assert lines[-1]["status"] == "DRY_RUN"
    assert lines[-1]["planned_action"]["action"] == "TYPE_TEXT"
