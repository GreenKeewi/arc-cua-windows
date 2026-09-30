from __future__ import annotations

import pytest

from arc_cua import ActionKind, DesktopElement, DesktopSnapshot, Subtask, TerminalKind
from arc_cua.policies import ChoicePolicy, TypeSafeJevPolicy


class FakeTransport:
    """Any provider that answers choice questions; no TypeSafe code involved."""

    name = "Fake"

    def __init__(self, selections):
        self.selections = selections
        self.calls = []

    def ask(self, state, questions):
        self.calls.append((state, questions))
        return {"answers": {
            key: {"choice": choice, "confidence": 0.9,
                  "probabilities": {option: float(option == choice) for option in questions[key]["criteria"]}}
            for key, choice in self.selections.items()
        }, "model": "fake-1"}


def snapshot(screenshot=None) -> DesktopSnapshot:
    return DesktopSnapshot(application="Editor", window="Document", revision="1", screenshot=screenshot, elements=(
        DesktopElement(id="save", role="Button", name="Save", actions=(ActionKind.CLICK,)),
        DesktopElement(id="cancel", role="Button", name="Cancel", actions=(ActionKind.CLICK,)),
    ))


def test_choice_policy_decides_through_any_transport() -> None:
    transport = FakeTransport({"operation": "CLICK", "click_target": "save", "click_modifier": "NONE"})
    decision = ChoicePolicy(transport).decide(
        subtask=Subtask(goal="Save", verification=("Saved",)), snapshot=snapshot(), history=(),
    )
    assert decision.kind == ActionKind.CLICK
    assert decision.target_id == "save"
    assert decision.confidence == 0.9
    assert decision.raw["model"] == "fake-1"
    state, questions = transport.calls[0]
    assert state["subtask"]["goal"] == "Save"
    assert set(questions["click_target"]["criteria"]) == {"save", "cancel"}


def test_choice_policy_matches_the_typesafe_request() -> None:
    task = Subtask(goal="Save", verification=("Saved",))
    transport = FakeTransport({"operation": "NEEDS_AGENT"})
    ChoicePolicy(transport).decide(subtask=task, snapshot=snapshot(), history=())
    policy = TypeSafeJevPolicy(api_key="test")
    try:
        assert transport.calls[0][1] == policy._build_questions(task, snapshot())[0]
    finally:
        policy.transport.client.close()


def test_invalid_answer_names_the_provider_and_executes_nothing() -> None:
    transport = FakeTransport({"operation": "CLICK", "click_target": "save"})
    transport.selections["click_target"] = "invented"
    with pytest.raises(ValueError, match="Invalid Fake choice response; no action executed"):
        ChoicePolicy(transport).decide(
            subtask=Subtask(goal="Save", verification=("Saved",)), snapshot=snapshot(), history=(),
        )


def test_terminal_answer_needs_no_target() -> None:
    decision = ChoicePolicy(FakeTransport({"operation": "BLOCKED"})).decide(
        subtask=Subtask(goal="Save", verification=("Saved",)), snapshot=snapshot(), history=(),
    )
    assert decision.terminal == TerminalKind.BLOCKED


def test_confidence_is_the_weakest_answer_the_decision_uses() -> None:
    transport = FakeTransport({"operation": "CLICK", "click_target": "save", "click_modifier": "NONE"})
    original = transport.ask

    def ask(state, questions, **kwargs):
        result = original(state, questions)
        result["answers"]["click_target"]["confidence"] = 0.3
        result["answers"]["scroll_direction"] = {"choice": "UP", "confidence": 0.01,
                                                 "probabilities": {"UP": 1.0, "DOWN": 0.0, "LEFT": 0.0, "RIGHT": 0.0}}
        return result

    transport.ask = ask
    decision = ChoicePolicy(transport).decide(
        subtask=Subtask(goal="Save", verification=("Saved",)), snapshot=snapshot(), history=(),
    )
    # The unused scroll head does not lower the click decision's confidence.
    assert decision.confidence == 0.3


class ImageTransport(FakeTransport):
    name = "Vision"
    supports_images = True

    def __init__(self, selections, image_selections):
        super().__init__(selections)
        self.image_selections = image_selections
        self.images = []

    def ask(self, state, questions, *, images=()):
        self.images.append(images)
        image_answers = FakeTransport(self.image_selections)
        result = (image_answers if images else super()).ask(state, questions)
        if images:
            self.calls.append(image_answers.calls[0])
        return result


@pytest.mark.parametrize(("image_answer", "terminal"), [
    ("SATISFIED", TerminalKind.SUBTASK_COMPLETE),
    ("NOT_SATISFIED", TerminalKind.NEEDS_AGENT),
])
def test_completion_checks_are_repeated_with_a_screenshot(image_answer, terminal) -> None:
    transport = ImageTransport({"operation": "SUBTASK_COMPLETE", "verification_0": "SATISFIED"},
                               {"verification_0": image_answer})
    decision = ChoicePolicy(transport, screenshot_checks=True).decide(
        subtask=Subtask(goal="Save", verification=("Saved",)), snapshot=snapshot(lambda: b"png"), history=(),
    )
    assert decision.terminal == terminal
    assert transport.images == [(), (b"png",)]
    state, questions = transport.calls[1]
    assert set(questions) == {"verification_0"}
    rules = questions["verification_0"]["instructions"]["rules"]
    assert "image of the window, captured with state.desktop.elements" in rules
    assert decision.raw["image_verification"]["answers"]["verification_0"]["choice"] == image_answer


def test_screenshots_are_not_taken_for_ordinary_steps() -> None:
    transport = ImageTransport({"operation": "CLICK", "click_target": "save", "click_modifier": "NONE"}, {})
    ChoicePolicy(transport, screenshot_checks=True).decide(
        subtask=Subtask(goal="Save", verification=("Saved",)),
        snapshot=snapshot(lambda: pytest.fail("no screenshot needed")), history=(),
    )
    assert transport.images == [()]


def test_screenshots_require_a_provider_that_accepts_images() -> None:
    with pytest.raises(ValueError, match="Fake does not accept images"):
        ChoicePolicy(FakeTransport({}), screenshot_steps=True)
    with pytest.raises(ValueError, match="Fake does not accept images"):
        ChoicePolicy(FakeTransport({}), screenshot_checks=True)
    policy = TypeSafeJevPolicy(api_key="test")
    try:
        with pytest.raises(ValueError, match="JEV does not accept images"):
            policy.transport.ask({}, {}, images=(b"png",))
    finally:
        policy.transport.client.close()


def test_completion_is_not_accepted_without_the_snapshot_pixels() -> None:
    transport = ImageTransport({"operation": "SUBTASK_COMPLETE", "verification_0": "SATISFIED"}, {})
    with pytest.raises(RuntimeError, match="Snapshot has no screenshot; no decision made"):
        ChoicePolicy(transport, screenshot_checks=True).decide(
            subtask=Subtask(goal="Save", verification=("Saved",)), snapshot=snapshot(), history=(),
        )


def test_margin_is_the_smallest_lead_among_the_answers_used() -> None:
    transport = FakeTransport({"operation": "CLICK", "click_target": "save", "click_modifier": "NONE"})
    original = transport.ask

    def ask(state, questions, **kwargs):
        result = original(state, questions)
        result["answers"]["click_target"]["probabilities"] = {"save": 0.55, "cancel": 0.45}
        return result

    transport.ask = ask
    decision = ChoicePolicy(transport).decide(
        subtask=Subtask(goal="Save", verification=("Saved",)), snapshot=snapshot(), history=(),
    )
    assert decision.margin == pytest.approx(0.1)


def test_screenshot_steps_attach_the_image_to_every_decision() -> None:
    selections = {"operation": "SUBTASK_COMPLETE", "verification_0": "SATISFIED"}
    transport = ImageTransport({}, selections)  # answers only requests that carry an image
    decision = ChoicePolicy(transport, screenshot_steps=True, screenshot_checks=True).decide(
        subtask=Subtask(goal="Save", verification=("Saved",)), snapshot=snapshot(lambda: b"png"), history=(),
    )
    # One request: the completion checks already saw the image.
    assert decision.terminal == TerminalKind.SUBTASK_COMPLETE
    assert transport.images == [(b"png",)]
    assert "image of the window" in transport.calls[0][0]["image"]


def field_snapshot() -> DesktopSnapshot:
    return DesktopSnapshot(application="Mail", window="New Message", revision="1", elements=(
        DesktopElement(id="to", role="TextField", name="To", value="",
                       actions=(ActionKind.CLICK, ActionKind.TYPE_TEXT)),
    ))


def test_no_fitting_input_hands_back_for_that_field() -> None:
    transport = FakeTransport({"operation": "TYPE_TEXT", "type_text_target": "to", "type_text_input": "NONE"})
    decision = ChoicePolicy(transport).decide(
        subtask=Subtask(goal="Email Sam", verification=("Sent",), inputs={"subject": "Lunch"}),
        snapshot=field_snapshot(), history=(),
    )
    assert decision.terminal == TerminalKind.NEEDS_INPUT
    assert decision.target_id == "to"
    assert "none of the supplied inputs" in decision.reason


def test_text_fields_are_offered_without_inputs_only_to_ask_for_text() -> None:
    transport = FakeTransport({"operation": "NEEDS_AGENT"})
    ChoicePolicy(transport).decide(
        subtask=Subtask(goal="Email Sam", verification=("Sent",)), snapshot=field_snapshot(), history=(),
    )
    questions = transport.calls[0][1]
    assert "TYPE_TEXT" in questions["operation"]["criteria"]
    assert set(questions["type_text_input"]["criteria"]) == {"NONE"}


def test_capping_keeps_controls_related_to_the_task_in_element_order() -> None:
    elements = tuple(
        DesktopElement(id=f"b{i}", role="Button", name=f"Filler {i}", actions=(ActionKind.CLICK,)) for i in range(10)
    ) + (
        DesktopElement(id="export", role="Button", name="Export as PDF", actions=(ActionKind.CLICK,)),
        DesktopElement(id="bold", role="Button", name="Bold", actions=(ActionKind.CLICK,), selected=True),
    )
    transport = FakeTransport({"operation": "NEEDS_AGENT"})
    ChoicePolicy(transport, max_candidates=4).decide(
        subtask=Subtask(goal="Export the document as a PDF", verification=("A PDF exists",)),
        snapshot=DesktopSnapshot(application="Editor", window="Doc", revision="1", elements=elements), history=(),
    )
    state, questions = transport.calls[0]
    assert list(questions["click_target"]["criteria"]) == ["b0", "b1", "export", "bold"]
    assert state["candidate_truncation"] == {"CLICK": 8}
