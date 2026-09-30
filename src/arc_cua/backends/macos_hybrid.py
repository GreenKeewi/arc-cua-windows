from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from functools import partial
from typing import Any

from ..errors import (
    StaleDesktopState,
    UnsupportedDesktopAction,
)
from ..models import (
    ActionKind,
    Bounds,
    DesktopElement,
    DesktopSnapshot,
    ExecutableAction,
)
from .macos_ax import _KEYCODES, MacOSAXBackend, _bounds_from_values, copy_attributes, is_modal, modifier_flags
from .macos_events import AXEventMonitor
from .macos_ocr import MacOSOCRProvider, _normalize_ocr_text, png_image, window_thumbnail

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, eq=False)
class VisualProbe:
    """Cheap settle probe: the app's front window, a small thumbnail, and an AX event count.

    Two probes are equal when the window is the same, no accessibility
    notification arrived in between, and almost no thumbnail pixels changed
    noticeably, so a blinking caret does not count as activity.
    """

    pid: int | None
    window_id: int | None
    bounds: tuple[float, float, float, float] | None
    pixels: bytes
    ax_events: int = 0

    PIXEL_DELTA = 24
    MAX_CHANGED_FRACTION = 0.002

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, VisualProbe):
            return NotImplemented
        if (self.pid, self.window_id, self.bounds, self.ax_events) != (
            other.pid, other.window_id, other.bounds, other.ax_events
        ):
            return False
        if len(self.pixels) != len(other.pixels):
            return False
        limit = max(1, int(len(self.pixels) * self.MAX_CHANGED_FRACTION))
        changed = 0
        for a, b in zip(self.pixels, other.pixels):
            if abs(a - b) > self.PIXEL_DELTA:
                changed += 1
                if changed > limit:
                    return False
        return True

    __hash__ = None  # type: ignore[assignment]  # tolerance-based equality


class MacOSHybridBackend:
    """
    Accessibility + Apple Vision OCR.

    AX gives us:
      - semantic controls
      - text fields
      - buttons
      - native actions

    OCR gives us:
      - visible text AX missed
      - screen coordinates

    Both become DesktopElement objects.

    Observes and controls one app, given by process ID, in the background; see
    MacOSAXBackend. Use it as a context manager to also reach a minimized window
    or a hidden app's windows, out of sight.
    """

    def __init__(
        self,
        pid: int,
        *,
        max_ax_elements: int = 1200,
        max_ax_depth: int = 18,
        ocr_recognition_level: str = "fast",
        ocr_min_confidence: float = 0.45,
        ocr_max_elements: int = 160,
        ocr: str = "auto",
        capture_screenshots: bool = False,
    ) -> None:
        """`ocr="auto"` reads the screen with OCR only when accessibility exposes no
        application control; `"always"` and `"never"` force it on or off. Without OCR
        no screenshot is taken unless `capture_screenshots` is set."""
        if ocr not in OCR_MODES:
            raise ValueError(f"ocr must be one of {sorted(OCR_MODES)}")
        self.ocr_mode = ocr
        self.capture_screenshots = capture_screenshots
        self._used_ocr = True

        self.ax = MacOSAXBackend(
            pid,
            max_elements=max_ax_elements,
            max_depth=max_ax_depth,
        )

        self.ocr = MacOSOCRProvider(
            recognition_level=
                ocr_recognition_level,
            min_confidence=
                ocr_min_confidence,
            max_elements=
                ocr_max_elements,
        )

        self._ocr_elements: dict[
            str,
            DesktopElement,
        ] = {}

        self._ax_events = AXEventMonitor()

        self.app = self.ax.app

    @property
    def pid(self) -> int:
        return self.app.pid

    def open(self) -> None:
        """Make sure the app has a window to work in; see ``MacOSApp.open``."""
        self.app.open()

    def close(self) -> None:
        self.app.close()

    def __enter__(self) -> MacOSHybridBackend:
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def observe(
        self,
    ) -> DesktopSnapshot:

        ax_snapshot = self.ax.observe()

        pid = int(
            ax_snapshot.context["pid"]
        )

        modal_elements, modal_bounds = _modal_elements(
            self.ax,
            pid,
            ax_snapshot,
        )

        modal_active = bool(
            modal_elements
        )

        if modal_active:
            # A modal blocks interaction with the content behind it.
            ax_elements = tuple(
                modal_elements
            )
        else:
            ax_elements = tuple(
                ax_snapshot.elements
            )

        # While a modal is active, don't force OCR to the old AX window title.
        # Let Quartz choose the app's front surface.
        preferred_title = (
            None
            if modal_active
            else ax_snapshot.window
        )

        use_ocr = self.ocr_mode == "always" or (
            self.ocr_mode == "auto" and not has_application_controls(ax_elements)
        )
        self._used_ocr = use_ocr
        ocr_capture = None
        image = None
        if use_ocr:
            ocr_capture = self.ocr.observe(
                pid=pid,
                app_name=ax_snapshot.application,
                preferred_window_title=preferred_title,
            )
            image = ocr_capture.image
            ocr_elements = _drop_ocr_seen_by_ax(
                _dedupe_ocr(
                    ocr_capture.elements
                ),
                ax_elements,
            )
        else:
            ocr_elements = ()
            if self.capture_screenshots:
                _, image = self.ocr.capture(pid=pid, preferred_window_title=preferred_title)

        if (
            use_ocr
            and modal_active
            and modal_bounds is not None
        ):
            # If the modal is attached inside the parent window, OCR may still
            # see the whole parent. Keep only text whose center is in the modal.
            ocr_elements = tuple(
                element
                for element in ocr_elements
                if (
                    element.bounds is not None
                    and _bounds_contains_point(
                        modal_bounds,
                        element.bounds.center,
                    )
                )
            )

        elements = (
            tuple(ax_elements)
            + tuple(ocr_elements)
        )

        self._ocr_elements = {
            element.id: element
            for element in ocr_elements
        }

        revision_payload = [
            {
                "id": element.id,
                "source": element.source,
                "guard": (
                    element.id
                    if element.source == "macos_ocr"
                    else element.semantic_guard()
                ),
            }
            for element in sorted(
                elements,
                key=lambda item: item.id,
            )
        ]

        revision_payload.append(
            {
                "modal_active": modal_active,
                "modal_bounds": _bounds_payload(
                    modal_bounds
                ),
            }
        )

        revision = hashlib.sha256(
            json.dumps(
                revision_payload,
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()

        context = dict(
            ax_snapshot.context
        )

        context.update(
            {
                "backend": "macos_hybrid",
                "ocr_window_id": ocr_capture.window_id if ocr_capture else None,
                "ocr_window_bounds": _bounds_payload(
                    ocr_capture.window_bounds if ocr_capture else None
                ),
                "perception_sources": ["macos_ax", "macos_ocr"] if use_ocr else ["macos_ax"],
                "modal_active": modal_active,
                "modal_bounds": _bounds_payload(
                    modal_bounds
                ),
                "modal_element_count": len(
                    modal_elements
                ),
            }
        )

        self.app.keep_behind()

        logger.debug(
            "hybrid observe ax=%d ocr=%d modal=%s",
            len(ax_elements), len(ocr_elements), modal_active,
        )

        return DesktopSnapshot(
            application=ax_snapshot.application,
            window=(
                (ocr_capture.window_title if ocr_capture else None)
                or ax_snapshot.window
            ),
            revision=revision,
            elements=elements,
            context=context,
            captured_at_ms=round(
                time.time() * 1000
            ),
            screenshot=(
                partial(png_image, image)
                if image is not None
                else None
            ),
        )


    def settle_probe(self) -> VisualProbe | int:
        """Lightweight signal for post-action settling (no AX walk, no OCR).

        When the last observation needed no OCR, the app's accessibility notification
        count is enough and no screen is captured; otherwise a window thumbnail too."""

        pid = self.app.pid
        self.app.keep_behind()
        events = (
            self._ax_events.count
            if self._ax_events.watch(pid)
            else 0
        )
        if not self._used_ocr:
            return events
        windows = self.app.windows()
        if not windows:
            return VisualProbe(pid, None, None, b"", events)
        window = windows[0]
        bounds = window.bounds
        # Only this app's windows: the user's windows covering it are not its reaction.
        return VisualProbe(
            pid,
            window.window_id,
            (bounds.x, bounds.y, bounds.width, bounds.height),
            window_thumbnail(bounds, window_ids=[w.window_id for w in windows]),
            events,
        )

    def is_fresh(
        self,
        snapshot: DesktopSnapshot,
        action: ExecutableAction,
    ) -> bool:

        # Keyboard shortcuts / scroll / wait
        # remain handled by AX.

        if action.target_id is None:

            return self.ax.is_fresh(
                snapshot,
                action,
            )

        try:
            target = snapshot.element(
                action.target_id
            )

        except KeyError:
            return False

        # Normal AX element.

        if target.source != "macos_ocr":

            return self.ax.is_fresh(
                snapshot,
                action,
            )

        # OCR target: make sure the app is
        # still running.

        self.app.check_running()

        if (
            self.app.pid
            != snapshot.context.get("pid")
        ):
            return False

        # And make sure we're still looking at
        # the same window.

        current_window = (
            self.ocr.front_window(
                int(
                    snapshot.context["pid"]
                ),
                preferred_title=
                    snapshot.window,
            )
        )

        if current_window is None:
            return False

        if (
            current_window.window_id
            != target.metadata.get(
                "window_id"
            )
        ):
            return False

        return True

    def execute(
        self,
        snapshot: DesktopSnapshot,
        action: ExecutableAction,
    ) -> None:

        if not self.is_fresh(
            snapshot,
            action,
        ):
            raise StaleDesktopState(
                "macOS hybrid target changed "
                "before execution"
            )

        # Global action.
        if action.target_id is None:

            self.ax.execute(
                snapshot,
                action,
            )

            return

        target = snapshot.element(
            action.target_id
        )

        # Normal accessibility target:
        # keep using AX native execution.

        if target.source != "macos_ocr":

            self.ax.execute(
                snapshot,
                action,
            )

            return

        # OCR target = coordinate execution.

        if target.bounds is None:

            raise UnsupportedDesktopAction(
                "OCR target has no screen bounds"
            )

        if action.kind == ActionKind.TYPE_TEXT:
            if action.value is None:
                raise UnsupportedDesktopAction(
                    "TYPE_TEXT requires an agent-supplied value"
                )

            # OCR gives us a visual focus target. action.value has already been
            # validated/resolved from Subtask.inputs by arc_cua.
            self.app.click(target.bounds)

            time.sleep(0.08)
            self.app.shortcut(("MOD",), "A", _KEYCODES["A"], modifier_flags(("MOD",)))
            time.sleep(0.08)
            self.app.type_text(str(action.value))
            return

        if action.kind == ActionKind.CLICK:

            self.app.click(
                target.bounds,
                flags=(
                    modifier_flags((action.click_modifier,))
                    if action.click_modifier
                    else 0
                ),
            )

            return

        if (
            action.kind
            == ActionKind.DOUBLE_CLICK
        ):

            self.app.click(
                target.bounds,
                count=2,
            )

            return

        if (
            action.kind
            == ActionKind.RIGHT_CLICK
        ):

            self.app.click(
                target.bounds,
                right=True,
            )

            return

        if (
            action.kind
            == ActionKind.DRAG_TO
        ):

            if not action.secondary_target_id:

                raise UnsupportedDesktopAction(
                    "DRAG_TO requires a "
                    "destination"
                )

            destination = snapshot.element(
                action.secondary_target_id
            )

            if destination.bounds is None:

                raise UnsupportedDesktopAction(
                    "Drag destination has "
                    "no screen bounds"
                )

            self.app.drag(
                target.bounds,
                destination.bounds,
            )

            return

        raise UnsupportedDesktopAction(
            "OCR targets currently support "
            "CLICK, DOUBLE_CLICK, RIGHT_CLICK "
            f"and DRAG_TO; got "
            f"{action.kind.value}"
        )


OCR_MODES = frozenset({"auto", "always", "never"})

# Roles that make an application control; windows, groups and text are not enough.
_CONTROL_ROLES = {
    "Button", "CheckBox", "RadioButton", "PopUpButton", "MenuButton", "ComboBox", "Link", "MenuItem",
    "Slider", "Incrementor", "DisclosureTriangle", "Tab", "TextField", "TextArea", "SearchField",
    "SecureTextField",
}
_TEXT_INPUT_ROLES = {"TextField", "TextArea", "SearchField", "SecureTextField", "ComboBox"}


def has_application_controls(elements: tuple[DesktopElement, ...]) -> bool:
    """Whether accessibility exposes at least one usable control of the app itself:
    an enabled, labelled control (text inputs need no label), not a title-bar button."""
    return any(
        element.role in _CONTROL_ROLES
        and element.enabled
        and element.actions
        and "window_control" not in element.metadata
        and (element.name.strip() or element.role in _TEXT_INPUT_ROLES)
        for element in elements
    )


def _dedupe_ocr(
    elements: tuple[DesktopElement, ...],
) -> tuple[DesktopElement, ...]:
    # Collapse overlapping Apple Vision observations.
    #
    # Vision can return multiple slightly different readings for the same visual
    # control/text region. Prefer the highest-confidence reading so JEV sees one
    # candidate instead of several competing copies.

    kept: list[DesktopElement] = []

    ordered = sorted(
        elements,
        key=lambda element: float(
            element.metadata.get("confidence", 0.0)
        ),
        reverse=True,
    )

    for candidate in ordered:
        if candidate.bounds is None:
            continue

        duplicate = False

        for existing in kept:
            if existing.bounds is None:
                continue

            if _iou(candidate.bounds, existing.bounds) >= 0.55:
                duplicate = True
                break

        if not duplicate:
            kept.append(candidate)

    return tuple(kept)


def _drop_ocr_seen_by_ax(
    ocr_elements: tuple[DesktopElement, ...],
    ax_elements: tuple[DesktopElement, ...],
) -> tuple[DesktopElement, ...]:
    # Drop OCR text that an actionable accessibility element already represents,
    # centered inside its bounds. The AX element has stronger semantics, and one
    # control should not be offered under two ids.
    #
    # Names match by containment: fast OCR often reads a truncated label ("Norm"
    # for a tab titled "Normal | ..."). Values must be mostly covered by the OCR
    # text: a field's own contents match, but one line of a terminal or document
    # whose value holds all of its text does not, since OCR may be the only way
    # to target that line.

    labelled = [
        (
            element.bounds,
            _normalize_ocr_text(element.name or ""),
            _normalize_ocr_text(element.value) if isinstance(element.value, str) else "",
        )
        for element in ax_elements
        if element.visible and element.actions and element.bounds is not None
    ]

    kept = []
    for element in ocr_elements:
        text = _normalize_ocr_text(element.name)
        if element.bounds is not None and len(text) >= 3 and any(
            (text in name or (text in value and 2 * len(text) >= len(value)))
            and _bounds_contains_point(bounds, element.bounds.center)
            for bounds, name, value in labelled
        ):
            continue
        kept.append(element)
    return tuple(kept)


def _iou(
    a: Bounds,
    b: Bounds,
) -> float:
    left = max(a.x, b.x)
    top = max(a.y, b.y)
    right = min(a.x + a.width, b.x + b.width)
    bottom = min(a.y + a.height, b.y + b.height)

    width = max(0.0, right - left)
    height = max(0.0, bottom - top)

    intersection = width * height

    if intersection <= 0.0:
        return 0.0

    union = (
        a.width * a.height
        + b.width * b.height
        - intersection
    )

    if union <= 0.0:
        return 0.0

    return intersection / union


def _ax_framework() -> Any:
    try:
        import ApplicationServices as AX  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "ApplicationServices is required for macOS modal observation. "
            "Install: pip install 'arc-cua[macos]'"
        ) from exc

    return AX


# AX attribute names are plain strings ("AXRole", "AXChildren", ...). Use them
# directly: a getattr on a constant PyObjC does not export (kAXSheetsAttribute)
# rescans framework metadata on every call, which dominated observation time.
def _ax_copy_attribute(
    AX: Any,
    element: Any,
    attribute: str,
) -> Any:
    try:
        result = AX.AXUIElementCopyAttributeValue(
            element,
            attribute,
            None,
        )
    except Exception:
        return None

    if isinstance(
        result,
        tuple,
    ):
        if len(result) < 2:
            return None

        try:
            error_code = int(
                result[0]
            )
        except Exception:
            error_code = 0

        if error_code != 0:
            return None

        return result[1]

    return result


def _ax_values(
    value: Any,
) -> list[Any]:
    if value is None:
        return []

    if isinstance(
        value,
        (
            list,
            tuple,
        ),
    ):
        return list(value)

    try:
        return list(value)
    except Exception:
        return [value]


def _ax_role(
    AX: Any,
    element: Any,
) -> str:
    value = _ax_copy_attribute(
        AX,
        element,
        "AXRole",
    )

    return str(
        value or ""
    )


def _ax_children(
    AX: Any,
    element: Any,
) -> list[Any]:
    children: list[Any] = []

    for attribute in (
        "AXSheets",
        "AXChildren",
    ):
        value = _ax_copy_attribute(
            AX,
            element,
            attribute,
        )

        children.extend(
            _ax_values(value)
        )

    return children


def _modal_elements(
    ax_backend: Any,
    pid: int,
    snapshot: DesktopSnapshot,
) -> tuple[tuple[DesktopElement, ...], Bounds | None]:
    """The elements of what blocks the observed window, and its bounds.

    Sheets and popovers live inside their window's tree, and the walk records them;
    a sheet on another window does not block this one. App-wide dialogs and alerts
    are windows of their own, so each app window's role is checked once. Help tags
    (tooltips) listed among the windows never count."""
    found = list(getattr(ax_backend, "modal_roots", ()))
    AX = _ax_framework()
    try:
        app = AX.AXUIElementCreateApplication(pid)
    except Exception:
        app = None
    known = {ax_backend.identity_for(ref) for ref, _, _ in found}
    for window in _ax_values(_ax_copy_attribute(AX, app, "AXWindows")) if app is not None else ():
        attributes = copy_attributes(AX, window, ("AXRole", "AXSubrole", "AXModal", "AXPosition", "AXSize"))
        if attributes is None or str(attributes["AXRole"] or "") == "AXHelpTag":
            continue
        identity = ax_backend.identity_for(window)
        if is_modal(str(attributes["AXRole"] or ""), attributes) and identity not in known:
            known.add(identity)
            bounds = _bounds_from_values(AX, attributes["AXPosition"], attributes["AXSize"])
            found.append((window, bounds, False))
    if not found:
        return (), None
    if all(is_root for _, _, is_root in found):
        # The observed window is itself the sheet or dialog: its elements are the modal's.
        return tuple(snapshot.elements), _union_bounds([b for _, b, _ in found if b is not None])
    return _collect_modal_ax_elements(ax_backend, [ref for ref, _, is_root in found if not is_root])


def _collect_modal_ax_elements(
    ax_backend: Any,
    roots: list[Any],
) -> tuple[
    tuple[DesktopElement, ...],
    Bounds | None,
]:
    AX = _ax_framework()

    elements: list[
        DesktopElement
    ] = []

    root_bounds: list[
        Bounds
    ] = []

    queue: deque[
        tuple[
            Any,
            str | None,
            int,
            bool,
        ]
    ] = deque(
        (
            root,
            None,
            0,
            True,
        )
        for root in roots
    )

    visited: set[str] = set()

    max_elements = 400
    max_depth = 14

    while (
        queue
        and len(elements) < max_elements
    ):
        (
            ref,
            parent_id,
            depth,
            is_root,
        ) = queue.popleft()

        element_id = ax_backend.identity_for(ref)

        if element_id in visited:
            continue

        visited.add(
            element_id
        )

        try:
            ax_backend.register_ref(
                element_id, ref
            )
        except Exception:
            pass

        try:
            element = ax_backend._element_from_ref(
                ref,
                element_id,
                parent_id=parent_id,
            )
        except Exception:
            element = None

        next_parent = parent_id

        if element is not None:
            elements.append(
                element
            )
            next_parent = (
                element.id
            )

            if (
                is_root
                and element.bounds is not None
            ):
                root_bounds.append(
                    element.bounds
                )

        if depth >= max_depth:
            continue

        for child in _ax_children(
            AX,
            ref,
        ):
            queue.append(
                (
                    child,
                    next_parent,
                    depth + 1,
                    False,
                )
            )

    modal_bounds = _union_bounds(
        root_bounds
    )

    if modal_bounds is None:
        modal_bounds = _union_bounds(
            [
                element.bounds
                for element in elements
                if element.bounds is not None
            ]
        )

    return (
        tuple(elements),
        modal_bounds,
    )


def _union_bounds(
    bounds_list: list[Bounds],
) -> Bounds | None:
    if not bounds_list:
        return None

    left = min(
        bounds.x
        for bounds in bounds_list
    )
    top = min(
        bounds.y
        for bounds in bounds_list
    )
    right = max(
        bounds.x + bounds.width
        for bounds in bounds_list
    )
    bottom = max(
        bounds.y + bounds.height
        for bounds in bounds_list
    )

    return Bounds(
        x=left,
        y=top,
        width=max(
            1.0,
            right - left,
        ),
        height=max(
            1.0,
            bottom - top,
        ),
    )


def _bounds_contains_point(
    bounds: Bounds,
    point: tuple[float, float],
) -> bool:
    x, y = point

    return (
        bounds.x <= x <= (
            bounds.x + bounds.width
        )
        and bounds.y <= y <= (
            bounds.y + bounds.height
        )
    )


def _bounds_payload(
    bounds: Bounds | None,
) -> dict[str, float] | None:

    if bounds is None:
        return None

    return {
        "x": round(bounds.x, 1),
        "y": round(bounds.y, 1),
        "width": round(
            bounds.width,
            1,
        ),
        "height": round(
            bounds.height,
            1,
        ),
    }
