from __future__ import annotations

import json

import httpx
import pytest

from arc_cua import ActionKind, DesktopElement, DesktopSnapshot, Subtask
from arc_cua.policies import TypeSafeJevPolicy


def test_compact_request_preserves_elements_and_all_offered_targets() -> None:
    captured = []

    def respond(request):
        body = json.loads(request.content)
        captured.append(body)
        choices = body["questions"]["operation"]["criteria"]
        return httpx.Response(200, json={"answers": {"operation": {
            "choice": "NEEDS_AGENT", "confidence": 1.0,
            "probabilities": {key: float(key == "NEEDS_AGENT") for key in choices},
        }}})

    snapshot = DesktopSnapshot(application="Files", window="Inbox", revision="1", elements=tuple(
        DesktopElement(id=f"item_{index}", role="TextField", name=f"Report {index}.pdf",
                       value=f"Report {index}.pdf", actions=(ActionKind.CLICK, ActionKind.DOUBLE_CLICK,
                                                          ActionKind.RIGHT_CLICK),
                       selected=index == 3, focused=index == 3, parent_id="list", source="accessibility",
                       metadata={"url": f"file:///Users/example/Desktop/Inbox/Report%20{index}.pdf"})
        for index in range(40)
    ) + (DesktopElement(id="hidden", role="Button", visible=False, actions=(ActionKind.CLICK,)),))
    task = Subtask(goal="Organize the reports", verification=("Reports are filed",), inputs={"folder": "Reports"})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        policy = TypeSafeJevPolicy(api_key="test", client=client)
        policy.decide(subtask=task, snapshot=snapshot, history=())
    body = captured[0]
    desktop = body["state"]["desktop"]
    decoded = [{k: v for k, v in zip(desktop["element_columns"], row) if v is not None}
               for row in desktop["elements"]]
    assert decoded == [element.compact() for element in snapshot.elements if element.visible]
    expected_ids = {element.id for element in snapshot.elements if element.visible}
    for head in ("click_target", "double_click_target", "right_click_target"):
        assert set(body["questions"][head]["criteria"]) == expected_ids
    assert body["state"]["subtask"] == task.compact()
    assert {k for k in body["questions"] if k.startswith("verification_")} == {"verification_0"}
    # Compare against the previous repeated-element representation of this same state.
    expanded = json.loads(json.dumps(body))
    expanded["state"]["desktop"] = {"elements": decoded}
    for head, question in expanded["questions"].items():
        question["instructions"]["subtask"] = task.compact()
        if head.endswith("_target"):
            question["criteria"] = {element["id"]: element for element in decoded}
    assert len(json.dumps(body)) < len(json.dumps(expanded)) * 0.6


def test_provider_token_limit_error_is_actionable_and_does_not_echo_response_text() -> None:
    def respond(request):
        return httpx.Response(400, json={"detail": {"error_type": "max_tokens_exceeded",
                                                   "message": "secret-provider-content"}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        policy = TypeSafeJevPolicy(api_key="test", client=client)
        with pytest.raises(RuntimeError, match="max_tokens_exceeded") as error:
            policy.transport._post({})
    assert "no action executed" in str(error.value)
    assert "secret-provider-content" not in str(error.value)


def test_dynamic_questions_use_observed_targets_and_agent_inputs() -> None:
    policy = TypeSafeJevPolicy(api_key="test")
    task = Subtask(
        goal="Search effects",
        verification=("The field contains Gaussian Blur",),
        inputs={"effect_name": "Gaussian Blur"},
    )
    snap = DesktopSnapshot(
        application="Editor",
        window="Effects",
        revision="1",
        elements=(
            DesktopElement(
                id="search",
                role="text_field",
                name="Search Effects",
                actions=(ActionKind.CLICK, ActionKind.TYPE_TEXT),
            ),
            DesktopElement(
                id="clip",
                role="clip",
                name="A.mov",
                actions=(ActionKind.CLICK,),
            ),
        ),
    )

    questions, maps, _ = policy._build_questions(task, snap)
    assert set(maps["TYPE_TEXT_target"]) == {"search"}
    assert set(maps["TYPE_TEXT_input"]) == {"effect_name", "NONE"}
    assert questions["type_text_input"]["criteria"]["effect_name"]["value"] == "Gaussian Blur"
    assert "Gaussian Blur" not in maps["operation"]


def test_policy_history_preserves_the_keys_that_were_actually_executed(monkeypatch) -> None:
    from arc_cua import Decision
    from arc_cua.models import ActionRecord, ExecutableAction

    policy = TypeSafeJevPolicy(api_key="test")
    captured = []

    def respond(body):
        captured.append(body)
        choices = body["questions"]["operation"]["criteria"]
        return {"answers": {"operation": {
            "choice": "NEEDS_AGENT", "confidence": 1.0,
            "probabilities": {key: float(key == "NEEDS_AGENT") for key in choices},
        }}}

    monkeypatch.setattr(policy.transport, "_post", respond)
    history = [
        ActionRecord(step=index, decision=Decision(kind=action.kind), action=action,
                     before_revision="same", after_revision="same", state_changed=False, elapsed_ms=10)
        for index, action in enumerate([
            ExecutableAction(kind=ActionKind.HOTKEY, hotkey="MOD+S"),
            ExecutableAction(kind=ActionKind.PRESS_KEY, key="ENTER"),
            ExecutableAction(kind=ActionKind.SCROLL, scroll_direction="DOWN"),
        ], start=1)
    ]
    try:
        policy.decide(subtask=Subtask(goal="Save", verification=("Saved",)),
                      snapshot=DesktopSnapshot(application="Editor", window="Document", revision="same", elements=()),
                      history=history)
    finally:
        policy.transport.client.close()
    actions = captured[0]["state"]["recent_actions"]
    assert actions[0]["hotkey"] == "MOD+S"
    assert actions[1]["key"] == "ENTER"
    assert actions[2]["scroll_direction"] == "DOWN"


@pytest.mark.parametrize(("criterion_result", "terminal"), [
    ("SATISFIED", "SUBTASK_COMPLETE"),
    ("NOT_SATISFIED", "NEEDS_AGENT"),
    ("UNKNOWN", "NEEDS_AGENT"),
])
def test_completion_requires_all_criterion_checks(monkeypatch, criterion_result, terminal) -> None:
    from arc_cua import DesktopExecutor
    from arc_cua.backends import StateMachineBackend

    policy = TypeSafeJevPolicy(api_key="test")

    def respond(body):
        selections = {"operation": "SUBTASK_COMPLETE", "verification_0": "SATISFIED",
                      "verification_1": criterion_result}
        return {"answers": {
            key: {"choice": choice, "confidence": 1.0,
                  "probabilities": {option: float(option == choice)
                                    for option in body["questions"][key]["criteria"]}}
            for key, choice in selections.items()
        }}

    monkeypatch.setattr(policy.transport, "_post", respond)
    snapshot = DesktopSnapshot(application="Files", window="Parent", revision="same", elements=())
    backend = StateMachineBackend(initial_state={}, snapshot_factory=lambda state: snapshot,
                                  transition=lambda state, action: pytest.fail("Unexpected UI action"))
    task = Subtask(goal="Create and open child", verification=("Child exists", "Child is open"))
    try:
        result = DesktopExecutor(backend, policy).run(task)
    finally:
        policy.transport.client.close()
    assert result.status.value == terminal
    if terminal == "NEEDS_AGENT":
        assert "Child is open" in result.reason
        assert criterion_result in result.reason
        assert not result.observations


@pytest.mark.parametrize(("value", "expected"), [
    ("2026-08-06 - Client - Proposal.pdf", {"text"}),
    ("0.5", {"text", "number"}),
    ("true", {"text", "boolean"}),
])
def test_set_value_does_not_offer_controls_incompatible_with_supplied_literals(value, expected):
    policy = TypeSafeJevPolicy(api_key="test")
    snapshot = DesktopSnapshot(application="Editor", window="Settings", revision="1", elements=tuple(
        DesktopElement(id=kind, role="Control", actions=(ActionKind.SET_VALUE,), metadata={"value_type": kind})
        for kind in ("text", "number", "boolean")
    ))
    try:
        _, maps, _ = policy._build_questions(
            Subtask(goal="Set value", verification=("Value set",), inputs={"value": value}), snapshot,
        )
    finally:
        policy.transport.client.close()
    assert set(maps["SET_VALUE_target"]) == expected


@pytest.mark.parametrize(("then_key", "expected"), [("ENTER", "ENTER"), ("TAB", "TAB"), ("NONE", None)])
def test_type_text_can_submit_with_a_following_key(monkeypatch, then_key, expected) -> None:
    policy = TypeSafeJevPolicy(api_key="test")
    captured = []

    def answer(choice, choices):
        return {"choice": choice, "confidence": 1.0,
                "probabilities": {key: float(key == choice) for key in choices}}

    def respond(body):
        captured.append(body)
        questions = body["questions"]
        return {"answers": {
            "operation": answer("TYPE_TEXT", questions["operation"]["criteria"]),
            "type_text_target": answer("path", questions["type_text_target"]["criteria"]),
            "type_text_input": answer("folder_path", questions["type_text_input"]["criteria"]),
            "type_text_then_key": answer(then_key, questions["type_text_then_key"]["criteria"]),
        }}

    monkeypatch.setattr(policy.transport, "_post", respond)
    snapshot = DesktopSnapshot(application="Files", window="Go to Folder", revision="1", elements=(
        DesktopElement(id="path", role="text_field", name="Path", actions=(ActionKind.TYPE_TEXT,)),
    ))
    task = Subtask(goal="Open the folder", verification=("The folder is open",), inputs={"folder_path": "/tmp/x"})
    try:
        decision = policy.decide(subtask=task, snapshot=snapshot, history=())
    finally:
        policy.transport.client.close()
    assert set(captured[0]["questions"]["type_text_then_key"]["criteria"]) == {"NONE", "ENTER", "TAB"}
    assert decision.kind == ActionKind.TYPE_TEXT
    assert decision.key == expected


def test_type_text_submit_head_is_not_offered_without_inputs() -> None:
    policy = TypeSafeJevPolicy(api_key="test")
    snapshot = DesktopSnapshot(application="Files", window="Main", revision="1", elements=(
        DesktopElement(id="path", role="text_field", name="Path", actions=(ActionKind.TYPE_TEXT,)),
    ))
    try:
        questions, _, _ = policy._build_questions(Subtask(goal="Look", verification=("Seen",)), snapshot)
    finally:
        policy.transport.client.close()
    assert "type_text_then_key" not in questions


@pytest.mark.parametrize(("modifier", "expected"), [("MOD", "MOD"), ("SHIFT", "SHIFT"), ("NONE", None)])
def test_click_can_hold_a_selection_modifier(monkeypatch, modifier, expected) -> None:
    policy = TypeSafeJevPolicy(api_key="test")
    captured = []

    def answer(choice, choices):
        return {"choice": choice, "confidence": 1.0,
                "probabilities": {key: float(key == choice) for key in choices}}

    def respond(body):
        captured.append(body)
        questions = body["questions"]
        return {"answers": {
            "operation": answer("CLICK", questions["operation"]["criteria"]),
            "click_target": answer("row_2", questions["click_target"]["criteria"]),
            "click_modifier": answer(modifier, questions["click_modifier"]["criteria"]),
        }}

    monkeypatch.setattr(policy.transport, "_post", respond)
    snapshot = DesktopSnapshot(application="Files", window="Inbox", revision="1", elements=tuple(
        DesktopElement(id=f"row_{index}", role="row", name=f"Report {index}.pdf", actions=(ActionKind.CLICK,))
        for index in range(3)
    ))
    try:
        decision = policy.decide(subtask=Subtask(goal="Select reports", verification=("Selected",)),
                                 snapshot=snapshot, history=())
    finally:
        policy.transport.client.close()
    assert set(captured[0]["questions"]["click_modifier"]["criteria"]) == {"NONE", "MOD", "SHIFT"}
    assert decision.kind == ActionKind.CLICK
    assert decision.click_modifier == expected
