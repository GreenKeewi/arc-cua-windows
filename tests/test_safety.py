from __future__ import annotations

import json

import pytest

from arc_cua import ActionKind, Decision, DesktopElement, DesktopExecutor, DesktopSnapshot, Subtask, TerminalKind
from arc_cua.api import result_to_dict
from arc_cua.backends import StateMachineBackend
from arc_cua.policies import ChoicePolicy, ScriptedPolicy
from arc_cua.safety import risks_of


@pytest.mark.parametrize(("label", "risks"), [
    ("Delete alarm", {"delete"}),
    ("Send", {"send"}),
    ("Pay now", {"purchase"}),
    ("Sign out", {"close"}),
    ("Closed tickets", set()),
    ("Shared with me", set()),
    ("Search", set()),
])
def test_risks_are_recognized_by_whole_words(label, risks):
    assert risks_of(label) == risks


def test_subtask_validates_risks_and_secret_keys():
    with pytest.raises(ValueError, match="Unknown allowed_risks"):
        Subtask(goal="x", verification=("y",), allowed_risks=("launch",))
    with pytest.raises(ValueError, match="not inputs"):
        Subtask(goal="x", verification=("y",), secret_inputs=("password",))


class Recorder:
    """Transport that records the request and asks for a fixed operation."""

    name = "Recorder"

    def __init__(self, selections):
        self.selections, self.calls = selections, []

    def ask(self, state, questions, *, images=()):
        self.calls.append((state, questions))
        return {"answers": {
            key: {"choice": choice, "confidence": 0.9,
                  "probabilities": {option: float(option == choice) for option in questions[key]["criteria"]}}
            for key, choice in self.selections.items()
        }}


def mail_snapshot(state=None) -> DesktopSnapshot:
    state = state or {"password": "", "sent": False}
    return DesktopSnapshot(application="Mail", window="Account", revision=json.dumps(state, sort_keys=True), elements=(
        DesktopElement(id="password", role="TextField", name="Password", value=state["password"],
                       actions=(ActionKind.CLICK, ActionKind.TYPE_TEXT)),
        DesktopElement(id="delete", role="Button", name="Delete account", actions=(ActionKind.CLICK,)),
        DesktopElement(id="save", role="Button", name="Save", actions=(ActionKind.CLICK,)),
    ))


@pytest.mark.parametrize(("allowed", "offered"), [
    ((), {"password", "save"}),
    (("delete",), {"password", "save", "delete"}),
])
def test_risky_controls_are_offered_only_when_allowed(allowed, offered):
    transport = Recorder({"operation": "NEEDS_AGENT"})
    ChoicePolicy(transport).decide(
        subtask=Subtask(goal="Update the account", verification=("Saved",), allowed_risks=allowed),
        snapshot=mail_snapshot(), history=(),
    )
    assert set(transport.calls[0][1]["click_target"]["criteria"]) == offered


def transition(state, action):
    if action.kind == ActionKind.TYPE_TEXT:
        state["password"] = action.value
    if action.kind == ActionKind.CLICK and action.target_id == "delete":
        state["sent"] = True


def test_runtime_refuses_a_risky_click_from_any_policy():
    backend = StateMachineBackend({"password": "", "sent": False}, mail_snapshot, transition)
    policy = ScriptedPolicy([Decision(kind=ActionKind.CLICK, target_id="delete")])
    result = DesktopExecutor(backend, policy).run(Subtask(goal="Tidy up", verification=("Tidy",)))
    assert result.status == TerminalKind.NEEDS_AGENT
    assert "Delete account" in result.reason and "delete" in result.reason
    assert backend.state["sent"] is False


def test_secret_values_never_reach_the_model_but_reach_the_app():
    task = Subtask(goal="Sign in", verification=("Signed in",), inputs={"pw": "hunter2", "user": "sam"},
                   secret_inputs=("pw",))
    backend = StateMachineBackend({"password": "", "sent": False}, mail_snapshot, transition)
    policy = ScriptedPolicy([Decision(kind=ActionKind.TYPE_TEXT, target_id="password", input_key="pw"),
                             Decision(terminal=TerminalKind.SUBTASK_COMPLETE)])
    result = DesktopExecutor(backend, policy).run(task)
    assert backend.state["password"] == "hunter2"  # the app gets the real value

    # Everything a decision model would see after that step is redacted.
    transport = Recorder({"operation": "NEEDS_AGENT"})
    ChoicePolicy(transport).decide(subtask=task, snapshot=backend.observe(), history=result.history)
    state, questions = transport.calls[0]
    assert "hunter2" not in json.dumps([state, questions])
    assert state["subtask"]["inputs"] == {"pw": "[secret]", "user": "sam"}
    assert questions["type_text_input"]["criteria"]["pw"]["value"] == "[secret]"

    # So is the evidence returned to the agent.
    assert "hunter2" not in json.dumps(result_to_dict(result), default=str)
