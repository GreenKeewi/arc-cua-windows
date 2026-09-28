from __future__ import annotations

from arc_cua import ActionKind, DesktopElement, DesktopSnapshot
from arc_cua.runtime import _structural_signature


def _snap(elements: tuple[DesktopElement, ...], revision: str = "1") -> DesktopSnapshot:
    return DesktopSnapshot(
        application="App",
        window="Win",
        revision=revision,
        elements=elements,
    )


def test_identical_snapshots_match() -> None:
    el = DesktopElement(id="a", role="button", name="OK", source="test", actions=(ActionKind.CLICK,))
    s1 = _snap((el,))
    s2 = _snap((el,))
    assert _structural_signature(s1) == _structural_signature(s2)


def test_value_change_differs_for_ax() -> None:
    common = dict(id="f", role="text_field", name="Field", source="macos_ax", actions=(ActionKind.TYPE_TEXT,))
    e1 = DesktopElement(value="hello", **common)
    e2 = DesktopElement(value="world", **common)
    assert _structural_signature(_snap((e1,))) != _structural_signature(_snap((e2,)))


def test_ocr_text_change_ignored() -> None:
    e1 = DesktopElement(id="o", role="visible_text", name="Hello", source="macos_ocr", actions=(ActionKind.CLICK,))
    e2 = DesktopElement(id="o", role="visible_text", name="Helllo", source="macos_ocr", actions=(ActionKind.CLICK,))
    assert _structural_signature(_snap((e1,))) == _structural_signature(_snap((e2,)))


def test_invisible_elements_excluded() -> None:
    visible = DesktopElement(id="v", role="button", name="OK", source="test", visible=True, actions=(ActionKind.CLICK,))
    hidden = DesktopElement(id="h", role="button", name="No", source="test", visible=False, actions=(ActionKind.CLICK,))
    sig_with = _structural_signature(_snap((visible, hidden)))
    sig_without = _structural_signature(_snap((visible,)))
    assert sig_with == sig_without


class _ProbeBackend:
    """Backend whose settle probe follows a scripted sequence of values."""

    def __init__(self, probes: list[int]) -> None:
        self.probes = probes
        self.calls = 0
        self.observations = 0

    def settle_probe(self) -> int:
        value = self.probes[min(self.calls, len(self.probes) - 1)]
        self.calls += 1
        return value

    def observe(self) -> DesktopSnapshot:
        self.observations += 1
        return _snap(())


def _settle(probes: list[int], **config) -> tuple[float, _ProbeBackend]:
    import time

    from arc_cua import RuntimeConfig
    from arc_cua.runtime import DesktopExecutor

    backend = _ProbeBackend(probes)
    executor = DesktopExecutor(backend, policy=None, config=RuntimeConfig(settle_poll_s=0.001, **config))
    before = executor._probe()
    started = time.perf_counter()
    executor._settle_with_probe(before)
    return time.perf_counter() - started, backend


def test_probe_settle_without_reaction_waits_only_reaction_window() -> None:
    elapsed, backend = _settle([0], settle_reaction_s=0.05, settle_timeout_s=1.0)
    assert 0.05 <= elapsed < 0.5
    assert backend.observations == 1


def test_probe_settle_waits_for_quiet_after_change() -> None:
    # Changes for a few polls, then holds steady.
    elapsed, backend = _settle([0, 1, 2, 3, 4, 5, 5], settle_quiet_s=0.05, settle_timeout_s=1.0)
    assert 0.05 <= elapsed < 0.5
    assert backend.calls > 6
    assert backend.observations == 1


def test_probe_settle_is_bounded_by_timeout_for_continuous_change() -> None:
    import itertools

    counter = itertools.count()
    backend = _ProbeBackend([0])
    backend.settle_probe = lambda: next(counter)  # type: ignore[method-assign]

    import time

    from arc_cua import RuntimeConfig
    from arc_cua.runtime import DesktopExecutor

    executor = DesktopExecutor(backend, policy=None, config=RuntimeConfig(settle_poll_s=0.001, settle_timeout_s=0.1))
    started = time.perf_counter()
    executor._settle_with_probe(executor._probe())
    assert 0.1 <= time.perf_counter() - started < 0.5


def test_backend_without_probe_uses_snapshot_settling() -> None:
    from arc_cua.runtime import DesktopExecutor

    executor = DesktopExecutor(object(), policy=None)  # type: ignore[arg-type]
    assert executor._probe() is None


def test_visual_probe_tolerates_caret_sized_changes() -> None:
    from arc_cua.backends.macos_hybrid import VisualProbe

    base = bytes(96 * 64)
    caret = bytearray(base)
    caret[100] = 255  # one pixel, e.g. a blinking caret
    moved = bytearray(base)
    moved[:500] = b"\xff" * 500  # a real content change

    probe = lambda pixels, window_id=1: VisualProbe(10, window_id, (0.0, 0.0, 100.0, 100.0), bytes(pixels))
    assert probe(base) == probe(caret)
    assert probe(base) != probe(moved)
    assert probe(base) != probe(base, window_id=2)
