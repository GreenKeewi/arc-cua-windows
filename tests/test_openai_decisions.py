"""OpenAIDecisionsTransport against a mocked endpoint."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from arc_cua import ActionKind, DesktopElement, DesktopSnapshot, Subtask, TerminalKind
from arc_cua.policies import ChoicePolicy
from arc_cua.policies._openai_decisions import OpenAIDecisionsTransport


def snapshot(screenshot=None) -> DesktopSnapshot:
    return DesktopSnapshot(application="Chrome", window="Checkout", revision="1", screenshot=screenshot, elements=(
        DesktopElement(id="w1", role="button", name="Review", actions=(ActionKind.CLICK,)),
        DesktopElement(id="w2", role="button", name="Cancel", actions=(ActionKind.CLICK,)),
    ))


def mock(selections, captured, *, with_probabilities=False):
    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        decisions = []
        for question in body["questions"]:
            if question["id"] not in selections:
                continue
            choice = selections[question["id"]]
            decision = {"question_id": question["id"], "answer": choice, "confidence": 0.8}
            if with_probabilities:
                decision["probabilities"] = {a["id"]: float(a["id"] == choice) for a in question["answers"]}
            decisions.append(decision)
        return httpx.Response(200, json={"model": "gpt-6-luna-test", "decisions": decisions})
    return httpx.Client(transport=httpx.MockTransport(respond))


def test_choice_policy_decides_through_openai_without_distributions() -> None:
    captured = []
    client = mock({"operation": "CLICK", "click_target": "w2", "click_modifier": "NONE"}, captured)
    transport = OpenAIDecisionsTransport(api_key="test", client=client)
    decision = ChoicePolicy(transport).decide(
        subtask=Subtask(goal="Cancel the order", verification=("Order cancelled",)), snapshot=snapshot(), history=(),
    )
    assert (decision.kind, decision.target_id, decision.confidence, decision.margin) == (
        ActionKind.CLICK, "w2", 0.8, None,
    )
    body = captured[0]
    assert body["model"] == "gpt-6-luna"
    state = json.loads(body["input"][0]["content"][0]["text"])
    assert state["subtask"]["goal"] == "Cancel the order"
    heads = {q["id"]: q for q in body["questions"]}
    assert {a["id"] for a in heads["click_target"]["answers"]} == {"w1", "w2"}


def test_distributions_are_validated_when_present() -> None:
    client = mock({"operation": "CLICK", "click_target": "w1", "click_modifier": "NONE"}, [], with_probabilities=True)
    decision = ChoicePolicy(OpenAIDecisionsTransport(api_key="test", client=client)).decide(
        subtask=Subtask(goal="Review", verification=("Reviewed",)), snapshot=snapshot(), history=(),
    )
    assert decision.margin == 1.0


def test_invented_answer_is_rejected() -> None:
    client = mock({"operation": "CLICK", "click_target": "w9", "click_modifier": "NONE"}, [])
    with pytest.raises(ValueError, match="Invalid OpenAI Decisions choice response"):
        ChoicePolicy(OpenAIDecisionsTransport(api_key="test", client=client)).decide(
            subtask=Subtask(goal="Review", verification=("Reviewed",)), snapshot=snapshot(), history=(),
        )


def test_screenshot_checks_send_the_snapshot_png() -> None:
    captured = []
    client = mock({"operation": "SUBTASK_COMPLETE", "verification_0": "SATISFIED"}, captured)
    decision = ChoicePolicy(OpenAIDecisionsTransport(api_key="test", client=client), screenshot_checks=True).decide(
        subtask=Subtask(goal="Review", verification=("Reviewed",)), snapshot=snapshot(lambda: b"png-bytes"), history=(),
    )
    assert decision.terminal == TerminalKind.SUBTASK_COMPLETE
    image = captured[1]["input"][0]["content"][1]
    assert image["image_url"] == "data:image/png;base64," + base64.b64encode(b"png-bytes").decode()
    assert [q["id"] for q in captured[1]["questions"]] == ["verification_0"]


def test_errors_report_only_the_structured_code() -> None:
    def respond(request):
        return httpx.Response(400, json={"error": {"code": "too_many_answers", "message": "secret page text"}})

    transport = OpenAIDecisionsTransport(api_key="test", client=httpx.Client(transport=httpx.MockTransport(respond)))
    with pytest.raises(RuntimeError, match=r"HTTP 400 \(too_many_answers\); no action executed") as error:
        transport.ask({}, {})
    assert "secret page text" not in str(error.value)
