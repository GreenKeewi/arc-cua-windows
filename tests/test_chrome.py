"""Live tests for ChromeBackend against local fixture pages (headless; ARC_TEST_HEADFUL=1 for a window)."""

from __future__ import annotations

import functools
import http.server
import os
import threading
import time
from pathlib import Path

import pytest

from arc_cua import ActionKind, Decision, DesktopExecutor, Subtask, TerminalKind
from arc_cua.backends.cdp import find_chrome
from arc_cua.errors import StaleDesktopState

pytest.importorskip("websockets")
try:
    find_chrome()
except FileNotFoundError:
    pytest.skip("Chrome is not installed", allow_module_level=True)

from arc_cua.backends import ChromeBackend  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "browser"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/slow":
            time.sleep(1.0)
            body = b"Results loaded"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


@pytest.fixture(scope="module")
def site():
    handler = functools.partial(_QuietHandler, directory=str(FIXTURES))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture(scope="module")
def chrome():
    # ARC_TEST_HEADFUL=1 runs the suite in a visible window.
    backend = ChromeBackend.launch(headless=os.environ.get("ARC_TEST_HEADFUL") != "1")
    yield backend
    backend.close()


@pytest.fixture
def page(chrome, site):
    chrome.navigate(f"{site}/index.html")
    return chrome


def named(snapshot, name, role=None):
    matches = [e for e in snapshot.elements if e.name == name and (role is None or e.role == role)]
    assert len(matches) == 1, f"{name!r}: {[(e.id, e.role, e.name) for e in snapshot.elements]}"
    return matches[0]


def status(backend):
    """The fixture's result line, read directly (it may be scrolled out of view)."""
    result = backend._call("Runtime.evaluate", {  # noqa: SLF001
        "expression": "document.getElementById('result').textContent", "returnByValue": True,
    })
    return result["result"]["value"]


class NamePolicy:
    """Test policy: each step names its target instead of knowing ids in advance."""

    def __init__(self, steps):
        self.steps = list(steps)

    def decide(self, *, subtask, snapshot, history):
        if not self.steps:
            return Decision(terminal=TerminalKind.SUBTASK_COMPLETE)
        kind, target, extra = self.steps.pop(0)
        target_id = named(snapshot, target).id if target else None
        return Decision(kind=kind, target_id=target_id, **extra)


def run(backend, steps, **inputs):
    task = Subtask(goal="test", verification=("done",), inputs=inputs)
    return DesktopExecutor(backend, NamePolicy(steps)).run(task)


def test_observe_reports_roles_names_values_and_states(page):
    snapshot = page.observe()
    assert named(snapshot, "Destination").actions == (ActionKind.CLICK, ActionKind.TYPE_TEXT)
    cabin = named(snapshot, "Cabin")
    assert (cabin.role, cabin.value, cabin.actions) == ("combobox", "Economy", (ActionKind.SET_VALUE,))
    assert cabin.metadata["options"] == ["Economy", "Business", "First"]
    flexible = named(snapshot, "Flexible dates")
    assert (flexible.role, flexible.value) == ("checkbox", False)
    assert named(snapshot, "Shadow action").role == "button"
    assert named(snapshot, "Inside frame").role == "button"
    assert named(snapshot, "Weekend deals").actions == (ActionKind.CLICK,)
    assert named(snapshot, "Plan a trip").role == "heading"
    # Label text is the control's name, not a separate text element.
    assert [e.role for e in snapshot.elements if e.name == "Destination"] == ["textbox"]
    assert snapshot.context["more_below"] is True
    assert all(e.source == "chrome_dom" for e in snapshot.elements)


def test_form_flow_with_autocomplete_select_and_hidden_checkbox(page):
    result = run(page, [
        (ActionKind.TYPE_TEXT, "Destination", {"input_key": "city"}),
        (ActionKind.CLICK, "Zurich", {}),
        (ActionKind.SET_VALUE, "Cabin", {"input_key": "cabin"}),
        (ActionKind.CLICK, "Flexible dates", {}),
        (ActionKind.CLICK, "Search", {}),
    ], city="Zu", cabin="Business")
    assert result.status == TerminalKind.SUBTASK_COMPLETE, result.reason
    assert all(record.state_changed for record in result.history)
    assert status(page) == "Results for Zurich in Business (flexible)"


def test_typing_replaces_existing_text_and_editing_hotkeys_work(page):
    run(page, [(ActionKind.TYPE_TEXT, "Destination", {"input_key": "first"})], first="London")
    run(page, [(ActionKind.TYPE_TEXT, "Destination", {"input_key": "second"})], second="Lisbon")
    assert named(page.observe(), "Destination").value == "Lisbon"
    run(page, [
        (ActionKind.HOTKEY, None, {"hotkey": "MOD+A"}),
        (ActionKind.PRESS_KEY, None, {"key": "BACKSPACE"}),
    ])
    assert named(page.observe(), "Destination").value == ""


def test_shadow_dom_iframe_and_pointer_widgets_are_clickable(page):
    for name, expected in [("Shadow action", "Shadow clicked"), ("Inside frame", "Frame clicked"),
                           ("Weekend deals", "Card opened")]:
        run(page, [(ActionKind.CLICK, name, {})])
        assert status(page) == expected


def test_pointer_wrapper_around_a_control_is_not_a_second_target(page):
    snapshot = page.observe()
    assert named(snapshot, "Nonstop only").role == "radio"
    assert not [e for e in snapshot.elements if e.role == "clickable" and not e.name]


def test_checked_radio_offers_no_click(page):
    snapshot = page.observe()
    assert named(snapshot, "Any number of stops").actions == ()
    run(page, [(ActionKind.CLICK, "Nonstop only", {})])
    snapshot = page.observe()
    assert named(snapshot, "Nonstop only").actions == ()
    assert named(snapshot, "Any number of stops").actions == (ActionKind.CLICK,)


def test_settling_waits_for_content_the_page_is_fetching(page):
    result = run(page, [(ActionKind.CLICK, "Load results", {})])
    assert any(e.name == "Results loaded" for e in result.final_snapshot.elements)


def test_covered_elements_are_not_offered_until_uncovered(page):
    run(page, [(ActionKind.SCROLL, None, {"scroll_direction": "DOWN"})] * 3)
    bottom = named(page.observe(), "Bottom button")
    assert bottom.actions == () and bottom.metadata.get("covered") is True
    result = run(page, [(ActionKind.CLICK, "Accept cookies", {}), (ActionKind.CLICK, "Bottom button", {})])
    assert result.status == TerminalKind.SUBTASK_COMPLETE, result.reason
    assert status(page) == "Bottom reached"
    # The status message at the top of the page is still observed while scrolled away.
    message = named(page.observe(), "Bottom reached")
    assert (message.role, message.metadata.get("offscreen")) == ("status", True)


def test_javascript_dialog_is_observable_and_answerable(page):
    run(page, [(ActionKind.TYPE_TEXT, "Destination", {"input_key": "city"})], city="Oslo")
    result = run(page, [(ActionKind.CLICK, "Reset", {})])
    snapshot = result.final_snapshot
    assert snapshot.context["dialog"] == "confirm"
    assert named(snapshot, "Clear the form?").role == "alertdialog"
    assert {e.name for e in snapshot.elements if e.actions} == {"OK", "Cancel"}
    run(page, [(ActionKind.CLICK, "OK", {})])
    assert status(page) == "Cleared"
    assert named(page.observe(), "Destination").value == ""


def test_link_opening_a_new_tab_is_followed(chrome, site):
    chrome.navigate(f"{site}/index.html")
    run(chrome, [(ActionKind.CLICK, "Travel details", {})])
    snapshot = chrome.observe()
    assert snapshot.window == "Travel details"
    assert named(snapshot, "Baggage allowance is 23 kg.").role == "text"


def test_changed_target_is_stale(page):
    from arc_cua.models import ExecutableAction

    snapshot = page.observe()
    search = named(snapshot, "Search")
    click = ExecutableAction(kind=ActionKind.CLICK, target_id=search.id, target_guard=search.semantic_guard())
    assert page.is_fresh(snapshot, click) is True
    page._call("Runtime.evaluate", {"expression": "document.getElementById('search').textContent = 'Go'"})  # noqa: SLF001
    assert page.is_fresh(snapshot, click) is False
    with pytest.raises(StaleDesktopState):
        page.execute(snapshot, click)
    assert status(page) == ""
