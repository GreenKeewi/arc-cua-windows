from types import SimpleNamespace

import pytest

from arc_cua.backends import macos_app
from arc_cua.errors import UnsupportedDesktopAction
from arc_cua.models import Bounds


class Ref:
    def __init__(self, name, **attributes):
        self.name = name
        self.attributes = attributes


@pytest.fixture
def fake_ax(monkeypatch):
    """AX attributes come from Ref objects; ids from a dict; frames from AXFrame."""
    ids = {}
    monkeypatch.setattr(macos_app, "_attr", lambda ref, name: ref.attributes.get(name))
    monkeypatch.setattr(macos_app.background, "window_id", lambda ref: ids.get(ref.name))
    monkeypatch.setattr(macos_app, "_ax_frame", lambda ref: ref.attributes.get("AXFrame"))
    return ids


def test_ax_windows_are_found_by_their_window_id(fake_ax):
    first, second = Ref("first"), Ref("second")
    fake_ax.update(first=10, second=20)
    app = Ref("app", AXWindows=[first, second])
    assert macos_app.find_ax_window(app, 20, info=lambda wid: None) is second
    assert macos_app.find_ax_window(app, 30, info=lambda wid: None) is None


def test_a_window_without_an_id_is_matched_by_frame_and_title(fake_ax):
    frame = Bounds(100, 50, 400, 300)
    known = Ref("known")
    fake_ax.update(known=10)
    unidentified = Ref("new", AXFrame=Bounds(100.4, 50, 400, 300), AXTitle="Report")
    elsewhere = Ref("other", AXFrame=Bounds(600, 50, 400, 300), AXTitle="Report")
    app = Ref("app", AXWindows=[known, elsewhere, unidentified])
    info = {20: {"bounds": frame, "title": "Report"}}.get
    assert macos_app.find_ax_window(app, 20, info=info) is unidentified
    # Same frame, different title: not the same window.
    retitled = {20: {"bounds": frame, "title": "Draft"}}.get
    assert macos_app.find_ax_window(app, 20, info=retitled) is None


def _app_with(windows, sheets=()):
    app = object.__new__(macos_app.MacOSApp)
    app.name, app.pid = "Form", 123
    app.windows = lambda: list(windows)
    app.attached_windows = lambda wid: list(sheets)
    app.check_running = lambda: None
    app.key_window = lambda: windows[0]
    return app


def window(wid, x, y, w=300, h=200):
    return SimpleNamespace(window_id=wid, title=f"w{wid}", bounds=Bounds(x, y, w, h))


def test_input_goes_to_the_target_window_not_the_one_on_top(monkeypatch):
    monkeypatch.setattr(macos_app, "window_server_info", lambda wid: {"title": ""})
    front, target = window(1, 0, 0), window(2, 0, 0)  # Same place; 1 is on top.
    app = _app_with([front, target])
    assert app._input_window(2, (50, 50)) is target
    assert app._input_window(None, (50, 50)) is front


def test_a_sheet_on_the_target_window_takes_its_input(monkeypatch):
    monkeypatch.setattr(macos_app, "window_server_info", lambda wid: {"title": ""})
    target, sheet = window(2, 0, 0, 400, 300), window(3, 100, 0, 200, 120)
    app = _app_with([sheet, target], sheets=[sheet])
    assert app._input_window(2, (150, 50)) is sheet  # On the sheet.
    assert app._input_window(2, (20, 250)) is target  # Beside it, on the window.
    assert app._input_window(2) is sheet  # Keys go to the sheet.
    with pytest.raises(UnsupportedDesktopAction, match="outside"):
        app._input_window(2, (900, 900))
