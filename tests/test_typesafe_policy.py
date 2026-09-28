from __future__ import annotations

import pytest

from arc_cua import ActionKind, DesktopElement, DesktopSnapshot, Subtask
from arc_cua.policies import TypeSafeJevPolicy


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
    assert set(maps["TYPE_TEXT_input"]) == {"effect_name"}
    assert questions["type_text_input"]["criteria"]["effect_name"]["value"] == "Gaussian Blur"
    assert "Gaussian Blur" not in maps["operation"]


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

    monkeypatch.setattr(policy, "_post", respond)
    snapshot = DesktopSnapshot(application="Files", window="Go to Folder", revision="1", elements=(
        DesktopElement(id="path", role="text_field", name="Path", actions=(ActionKind.TYPE_TEXT,)),
    ))
    task = Subtask(goal="Open the folder", verification=("The folder is open",), inputs={"folder_path": "/tmp/x"})
    try:
        decision = policy.decide(subtask=task, snapshot=snapshot, history=())
    finally:
        policy.client.close()
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
        policy.client.close()
    assert "type_text_then_key" not in questions
