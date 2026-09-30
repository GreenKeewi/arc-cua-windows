from __future__ import annotations

from arc_cua import ActionKind, Bounds, DesktopElement
from arc_cua.backends.macos_hybrid import _drop_ocr_seen_by_ax


def ax(name, bounds, actions=(ActionKind.CLICK,), value=None):
    return DesktopElement(id=f"ax_{name}", role="AXButton", name=name, value=value, actions=actions,
                          bounds=bounds, source="macos_ax")


def ocr(text, bounds):
    return DesktopElement(id=f"ocr_{text}", role="visible_text", name=text, actions=(ActionKind.CLICK,),
                          bounds=bounds, source="macos_ocr")


def test_ocr_reading_of_an_actionable_ax_control_is_dropped() -> None:
    tab = ax("Normal | Applied research", Bounds(0, 0, 200, 30))
    kept = _drop_ocr_seen_by_ax((ocr("Norm", Bounds(10, 5, 40, 20)),), (tab,))
    assert kept == ()


def test_ocr_text_is_kept_when_ax_cannot_act_on_it_or_does_not_contain_it() -> None:
    label = ocr("Save", Bounds(10, 5, 40, 20))
    others = (
        ax("Save", Bounds(0, 0, 200, 30), actions=()),        # not actionable
        ax("Save", Bounds(300, 0, 200, 30)),                  # elsewhere on screen
        ax("Cancel", Bounds(0, 0, 200, 30)),                  # different text
    )
    assert _drop_ocr_seen_by_ax((label,), others) == (label,)
    short = ocr("OK", Bounds(10, 5, 20, 20))                  # too short to match safely
    assert _drop_ocr_seen_by_ax((short,), (ax("OK", Bounds(0, 0, 200, 30)),)) == (short,)


def test_ax_value_matches_only_when_the_ocr_text_covers_most_of_it() -> None:
    field = ax("", Bounds(0, 0, 300, 30), value="Get Lucky Daft Punk")
    assert _drop_ocr_seen_by_ax((ocr("Get Lucky Daft Punk", Bounds(10, 5, 150, 20)),), (field,)) == ()
    # One line of a terminal or document whose value holds all of its text stays targetable.
    terminal = ax("", Bounds(0, 0, 800, 600), value="$ ls\nREADME.md\nsrc\ntests\n$ git status\nOn branch master")
    line = ocr("README.md", Bounds(10, 20, 80, 16))
    assert _drop_ocr_seen_by_ax((line,), (terminal,)) == (line,)
