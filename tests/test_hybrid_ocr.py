from __future__ import annotations

from types import SimpleNamespace

import pytest

from arc_cua import ActionKind, Bounds, DesktopElement, DesktopSnapshot
from arc_cua.backends import macos_hybrid
from arc_cua.backends.macos_ocr import OCRCapture

WINDOW = DesktopElement(id="w", role="Window", name="Spotify")
TITLE_BAR = DesktopElement(id="zoom", role="Button", name="this button also has an action to zoom the window",
                           actions=(ActionKind.CLICK,), metadata={"window_control": "zoom"})
UNLABELLED = DesktopElement(id="g", role="Group", name="", value="", actions=(ActionKind.SET_VALUE,))
SAVE = DesktopElement(id="save", role="Button", name="Save", actions=(ActionKind.CLICK,))
SEARCH = DesktopElement(id="search", role="SearchField", name="", actions=(ActionKind.TYPE_TEXT,))


@pytest.mark.parametrize(("elements", "has_controls"), [
    ((WINDOW, TITLE_BAR, UNLABELLED), False),
    ((WINDOW, TITLE_BAR, SAVE), True),
    ((WINDOW, SEARCH), True),
    ((WINDOW, DesktopElement(id="off", role="Button", name="Save", enabled=False, actions=(ActionKind.CLICK,))), False),
])
def test_application_controls_exclude_window_chrome_and_unlabelled_elements(elements, has_controls):
    assert macos_hybrid.has_application_controls(elements) is has_controls


def hybrid(monkeypatch, elements, mode):
    ocr_calls = []

    class OCR:
        def observe(self, **kwargs):
            ocr_calls.append(kwargs)
            text = DesktopElement(id="ocr_1", role="visible_text", name="Play", source="macos_ocr",
                                  actions=(ActionKind.CLICK,), bounds=Bounds(10, 10, 40, 20))
            return OCRCapture(pid=1, app_name="App", window_id=9, window_title="App", window_bounds=None,
                              elements=(text,), captured_at_ms=0, image=None)

        def capture(self, **kwargs):
            raise AssertionError("no screenshot was requested")

    snapshot = DesktopSnapshot(application="App", window="App", revision="1", elements=elements, context={"pid": 1})
    monkeypatch.setattr(macos_hybrid, "_collect_modal_ax_elements", lambda ax, pid: ((), None))
    backend = object.__new__(macos_hybrid.MacOSHybridBackend)
    backend.ax = SimpleNamespace(observe=lambda: snapshot)
    backend.ocr = OCR()
    backend.ocr_mode, backend.capture_screenshots, backend._used_ocr = mode, False, True
    backend._ocr_elements = {}
    backend.app = SimpleNamespace(pid=1, keep_behind=lambda: None)
    backend._ax_events = SimpleNamespace(count=7, watch=lambda pid: True)
    return backend, ocr_calls


@pytest.mark.parametrize(("mode", "elements", "ocr_used"), [
    ("auto", (WINDOW, SAVE), False),
    ("auto", (WINDOW, TITLE_BAR, UNLABELLED), True),
    ("always", (WINDOW, SAVE), True),
    ("never", (WINDOW, UNLABELLED), False),
])
def test_ocr_runs_only_when_needed(monkeypatch, mode, elements, ocr_used):
    backend, ocr_calls = hybrid(monkeypatch, elements, mode)
    snapshot = backend.observe()
    assert bool(ocr_calls) is ocr_used
    assert ("macos_ocr" in snapshot.context["perception_sources"]) is ocr_used
    assert any(e.source == "macos_ocr" for e in snapshot.elements) is ocr_used
    if not ocr_used:
        assert snapshot.screenshot is None
        assert backend.settle_probe() == 7  # accessibility notifications, no screen capture


def test_unknown_ocr_mode_is_rejected():
    with pytest.raises(ValueError, match="ocr must be one of"):
        macos_hybrid.MacOSHybridBackend(1, ocr="sometimes")


@pytest.mark.parametrize(("center", "expected"), [
    ((0.1, 0.1), "top left"), ((0.5, 0.5), "middle center"), ((0.2, 0.95), "bottom left"), ((0.9, 0.4), "middle right"),
])
def test_ocr_text_position_is_a_coarse_place_in_the_window(center, expected):
    from arc_cua.backends.macos_ocr import _position

    assert _position(*center) == expected
