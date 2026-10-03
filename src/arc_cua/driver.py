"""A macOS driver session: observe apps and act on them in the background, no policy.

Anything can drive it: a decision model, a frontier agent, a script. It keeps one
backend per app and a journal of each app's structural changes, so an action is
checked against the app as it is when the action runs, not as it was observed:

* An action on a snapshot first looks for structural changes since that snapshot
  (a sheet, window or menu that came or went). If there are any, it does not act;
  it returns status ``"changed"`` with a fresh snapshot to decide on again.
* The target itself must still be the element that was observed (the backend's
  freshness check); otherwise the result is ``"stale"``, also with a fresh snapshot.

Nothing waits after an action by default. Use ``wait`` when the caller expects
the app to take a while to react.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, replace
from typing import Any, Callable

from .errors import StaleDesktopState, UnsupportedDesktopAction
from .models import ActionKind, DesktopSnapshot, ExecutableAction

_TARGETED = frozenset({
    ActionKind.CLICK, ActionKind.DOUBLE_CLICK, ActionKind.RIGHT_CLICK, ActionKind.TYPE_TEXT,
    ActionKind.SET_VALUE, ActionKind.DRAG_TO, ActionKind.DRAG_BY,
})
_MARKER = "changes_seen"


@dataclass(frozen=True, slots=True)
class ActResult:
    status: str  # "done", "changed" (structure changed since the snapshot) or "stale" (target changed)
    snapshot: DesktopSnapshot | None = None  # A fresh observation when the action did not run.
    changes: tuple[str, ...] = ()
    elapsed_ms: float = 0.0

    @property
    def done(self) -> bool:
        return self.status == "done"


class _App:
    def __init__(self, pid: int) -> None:
        from .backends.macos_ax import MacOSAXBackend
        from .backends.macos_changes import ChangeJournal

        # Journal first, so nothing that happens during the first observation is missed.
        self.journal = ChangeJournal(pid)
        self.backend = MacOSAXBackend(pid)
        self.backend.open()

    def close(self) -> None:
        self.journal.close()
        self.backend.close()


class Driver:
    """Policy-free session over macOS apps. Use as a context manager, or call ``close``."""

    def __init__(self, *, app_factory: Callable[[int], Any] | None = None) -> None:
        """``app_factory`` builds the per-app backend and journal; tests pass fakes."""
        if app_factory is None and sys.platform != "darwin":
            raise RuntimeError("arc_cua.Driver controls macOS apps and needs macOS")
        self._factory = app_factory or _App
        self._apps: dict[int, Any] = {}

    def __enter__(self) -> Driver:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        for app in self._apps.values():
            app.close()
        self._apps.clear()

    def release(self, pid: int) -> None:
        """Stop working with one app and put its windows back as they were."""
        app = self._apps.pop(pid, None)
        if app is not None:
            app.close()

    def _app(self, pid: int) -> Any:
        app = self._apps.get(pid)
        if app is None:
            app = self._apps[pid] = self._factory(pid)
        return app

    # ---- reading ---------------------------------------------------------------

    @staticmethod
    def apps() -> list[dict[str, Any]]:
        """Running apps with a user interface: pid, name, bundle_id, frontmost, hidden."""
        import AppKit  # type: ignore

        workspace = AppKit.NSWorkspace.sharedWorkspace()
        front = workspace.frontmostApplication()
        front_pid = int(front.processIdentifier()) if front is not None else None
        return [
            {
                "pid": int(app.processIdentifier()),
                "name": str(app.localizedName() or ""),
                "bundle_id": str(app.bundleIdentifier() or ""),
                "frontmost": int(app.processIdentifier()) == front_pid,
                "hidden": bool(app.isHidden()),
            }
            for app in workspace.runningApplications()
            if app.activationPolicy() == AppKit.NSApplicationActivationPolicyRegular
        ]

    @staticmethod
    def windows(pid: int) -> list[Any]:
        """The app's normal windows on screen, front to back."""
        from .backends.macos_ocr import on_screen_windows

        return on_screen_windows(pid)

    def observe(self, pid: int) -> DesktopSnapshot:
        app = self._app(pid)
        # Read the journal before the walk: a change during the walk counts as after it.
        marker = app.journal.sequence
        snapshot = app.backend.observe()
        return replace(snapshot, context={**snapshot.context, _MARKER: marker})

    def wait(self, snapshot: DesktopSnapshot, *, timeout_s: float = 1.0, quiet_s: float = 0.05) -> DesktopSnapshot:
        """Observe again once the app's structure changes after ``snapshot``, or after
        ``timeout_s``. After a change, waits until ``quiet_s`` pass with no further change."""
        pid = snapshot.context["pid"]
        app = self._app(pid)
        seen = snapshot.context.get(_MARKER, app.journal.sequence)
        if app.journal.wait_after(seen, timeout_s):
            last = app.journal.sequence
            while app.journal.wait_after(last, quiet_s):
                last = app.journal.sequence
        return self.observe(pid)

    # ---- acting ----------------------------------------------------------------

    def act(
        self,
        snapshot: DesktopSnapshot,
        kind: ActionKind | str,
        target: str | None = None,
        *,
        value: str | int | float | bool | None = None,
        key: str | None = None,
        hotkey: str | None = None,
        scroll_direction: str | None = None,
        click_modifier: str | None = None,
    ) -> ActResult:
        """Perform one action on an element of ``snapshot``, if the app has not changed under it."""
        started = time.perf_counter()
        kind = ActionKind(kind)
        pid = snapshot.context["pid"]
        app = self._app(pid)
        action = _action(snapshot, kind, target, value=value, key=key, hotkey=hotkey,
                         scroll_direction=scroll_direction, click_modifier=click_modifier)

        changes = app.journal.since(snapshot.context.get(_MARKER, app.journal.sequence))
        if changes:
            fresh = self.observe(pid)
            if {e.id for e in fresh.elements} != {e.id for e in snapshot.elements}:
                return ActResult(
                    "changed", fresh, tuple(f"{c.notification} {c.role}".strip() for c in changes),
                    (time.perf_counter() - started) * 1000,
                )
            # Announced late: the snapshot already showed the change. Act on the fresh one,
            # which the backend's references now belong to.
            snapshot = fresh
        try:
            app.backend.execute(snapshot, action)
        except StaleDesktopState:
            return ActResult("stale", self.observe(pid), (), (time.perf_counter() - started) * 1000)
        return ActResult("done", None, (), (time.perf_counter() - started) * 1000)


def _action(
    snapshot: DesktopSnapshot,
    kind: ActionKind,
    target: str | None,
    **fields: Any,
) -> ExecutableAction:
    element = None
    if kind in _TARGETED:
        if not target:
            raise UnsupportedDesktopAction(f"{kind.value} needs a target element id")
        try:
            element = snapshot.element(target)
        except KeyError as exc:
            raise UnsupportedDesktopAction(f"No element {target!r} in this snapshot") from exc
        if kind not in element.actions:
            raise UnsupportedDesktopAction(f"{kind.value} is not offered for {target} ({element.role})")
        if kind in {ActionKind.TYPE_TEXT, ActionKind.SET_VALUE} and fields.get("value") is None:
            raise UnsupportedDesktopAction(f"{kind.value} needs a value")
    return ExecutableAction(
        kind=kind,
        target_id=element.id if element else None,
        target_guard=element.semantic_guard() if element else None,
        **fields,
    )
