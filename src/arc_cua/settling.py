"""Waiting for an app to finish reacting to an action, shared by the runtime and the driver.

A probe is a cheap reading that changes while the app reacts: usually its count of
accessibility notifications. Settling waits for the probe to change (the app
reacted), then for it to stay unchanged for a while (the app went quiet), within a
cap. When nothing changes during the reaction window, the action had no effect the
probe can see.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .models import DesktopSnapshot


@dataclass(frozen=True, slots=True)
class SettleTiming:
    reaction_s: float = 0.6  # Give up this soon when nothing changes.
    quiet_s: float = 0.15  # After a change, done once nothing more changed for this long.
    timeout_s: float = 2.0  # Never wait longer.
    poll_s: float = 0.02
    look_every_s: float = 0.1  # How often ``look`` is asked while nothing has changed.


@dataclass(frozen=True, slots=True)
class Settled:
    last: Any  # The last probe read.
    reacted: bool  # The probe changed from the one before the action.
    timed_out: bool  # Still changing when the timeout ended the wait.
    elapsed_s: float
    stopped: bool = False  # ``stop`` ended the wait early.


def wait_for_quiet(
    probe: Callable[[], Any],
    before: Any,
    timing: SettleTiming,
    *,
    stop: Callable[[], bool] | None = None,
    look: Callable[[], bool] | None = None,
) -> Settled:
    """Poll ``probe`` until the app reacted and went quiet, or did not react in time.

    ``before`` is the probe read before the action. A probe that returns None ends
    the wait. ``stop`` is asked before each poll; True ends the wait early.
    ``look``, for apps that do not announce every change, is asked every
    ``look_every_s`` while the probe has not changed; True counts as a reaction."""
    started = time.perf_counter()
    previous = before
    changed = False
    quiet_since = started
    timed_out = False
    looked_at = started
    while True:
        if stop is not None and stop():
            return Settled(previous, changed, False, time.perf_counter() - started, stopped=True)
        time.sleep(timing.poll_s)
        current = probe()
        now = time.perf_counter()
        if current is None:
            break
        if current != previous:
            changed = changed or current != before
            quiet_since = now
        elif look is not None and not changed and now - looked_at >= timing.look_every_s:
            looked_at = now
            if look():
                changed = True
                quiet_since = now = time.perf_counter()
        previous = current
        elapsed = now - started
        if changed and now - quiet_since >= timing.quiet_s:
            break
        if not changed and elapsed >= timing.reaction_s:
            break
        if elapsed >= timing.timeout_s:
            timed_out = True
            break
    return Settled(previous, changed, timed_out, time.perf_counter() - started)


def snapshot_signature(
    snapshot: DesktopSnapshot,
) -> tuple:
    # Stable representation for telling whether an action changed what a window shows.
    #
    # For OCR, ignore recognized text, confidence, and tiny geometry changes.
    # Those can vary between Apple Vision passes even when the UI is identical.
    #
    # For semantic accessibility elements, include value/state because those
    # changes are meaningful.

    rows = []

    for element in snapshot.elements:
        if not element.visible:
            continue

        if element.source == "macos_ocr":
            rows.append(
                (
                    element.id,
                    element.role,
                    element.source,
                    tuple(action.value for action in element.actions),
                )
            )
        else:
            rows.append(
                (
                    element.id,
                    element.role,
                    element.source,
                    str(element.value),
                    element.focused,
                    element.selected,
                    element.expanded,
                    tuple(action.value for action in element.actions),
                )
            )

    return tuple(sorted(rows))
