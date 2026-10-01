from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from ..errors import StaleDesktopState, TargetUnavailable, UnsupportedDesktopAction
from ..keyboard import parse_hotkey
from ..models import ActionKind, Bounds, DesktopElement, DesktopSnapshot, ExecutableAction
from .macos_app import MacOSApp
from .macos_ax_cache import AXChangeFeed, AXNode, AXNodeCache
from .macos_background import window_id
from .macos_events import AXEventMonitor

logger = logging.getLogger(__name__)

# How long observe() waits for an app that momentarily shows no window.
_WINDOW_WAIT_S = 1.0
_TEXT_ROLES = {"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"}
_ROW_ROLES = {"AXRow", "AXCell"}


@dataclass(frozen=True)
class RowState:
    """The list, table or outline row an element is inside."""

    selected: bool | None
_ROW_CONTENT_ROLES = {"AXStaticText", "AXImage"}
_VALUE_ROLES = _TEXT_ROLES | {"AXSlider", "AXIncrementor"}


class MacOSAXBackend:
    """Experimental semantic backend for macOS Accessibility (AX).

    Observes and controls one app, given by process ID, whether or not it is
    frontmost. Input reaches it in the background: the user's pointer, front app
    and key window are left alone. Raises ``TargetUnavailable`` once the app quits
    or has no usable window. Use the backend as a context manager to also reach a
    minimized window or a hidden app's windows, out of sight.

    It intentionally does not use application scripting APIs. The first version
    handles AX-native activation/value entry plus keyboard/scroll events. Custom
    canvas semantics (timelines, node graphs, viewports) belong in a separate
    perception source that can later be merged into the same DesktopSnapshot.
    """

    def __init__(self, pid: int, *, max_elements: int = 1200, max_depth: int = 18, cache: bool = False) -> None:
        if sys.platform != "darwin":
            raise RuntimeError("MacOSAXBackend is only available on macOS")
        self.max_elements = max_elements
        self.max_depth = max_depth
        self._refs: dict[str, Any] = {}
        self._identities = _AXIdentityRegistry()
        self._full_tree_supported = True
        self._require_accessibility()
        self.app = MacOSApp(pid)
        # Opt-in: re-read only elements the app reports as changed. See macos_ax_cache.
        self._cache = AXNodeCache(AXChangeFeed(pid)) if cache else None
        self._events: AXEventMonitor | None = None

    @property
    def pid(self) -> int:
        return self.app.pid

    def open(self) -> None:
        """Make sure the app has a window to work in; see ``MacOSApp.open``."""
        self.app.open()

    def close(self) -> None:
        if self._cache is not None:
            self._cache.close()
            self._cache = None
        self.app.close()

    def __enter__(self) -> MacOSAXBackend:
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def settle_probe(self) -> int | None:
        """The app's accessibility notification count, for runtime settling.

        It changes within milliseconds of the app reacting to input and stays still
        once the app is idle, without capturing the screen. None when notifications
        are unavailable; the runtime then settles by comparing observations."""
        if self._events is None:
            self._events = AXEventMonitor()
        return self._events.count if self._events.watch(self.app.pid) else None

    def register_ref(self, element_id: str, ref: Any) -> None:
        self._refs[element_id] = ref

    def identity_for(self, ref: Any) -> str:
        return self._identities.id_for(ref)

    def observe(self) -> DesktopSnapshot:
        AS, _ = _frameworks()
        app_name = self.app.name
        # An app replacing its window (a folder opening, a document switching) briefly
        # shows none; wait for the new one before reporting the app unavailable.
        deadline = time.monotonic() + _WINDOW_WAIT_S
        while True:
            on_screen = {window.window_id for window in self.app.windows()}
            root = focused = None
            if on_screen:
                app_ref = self.app.ax
                self._request_full_tree(AS, app_ref)
                focused = _attr(AS, app_ref, "AXFocusedUIElement")
                candidates = [
                    _attr(AS, app_ref, "AXFocusedWindow"),
                    _attr(AS, app_ref, "AXMainWindow"),
                    _attr(AS, focused, "AXWindow") if focused is not None else None,
                    *(_attr(AS, app_ref, "AXWindows") or ()),
                ]
                # The focused window can be on another desktop; observe one the app shows here.
                root = next((w for w in candidates if w is not None and window_id(w) in on_screen), None)
            if root is not None:
                break
            if time.monotonic() >= deadline:
                if not on_screen:
                    self.app.require_window()  # Raises TargetUnavailable with the reason.
                self.app.check_running()
                raise TargetUnavailable(f"{app_name} exposes no on-screen window to accessibility.")
            time.sleep(0.05)
        self._identities.begin_observation()
        pid = self.app.pid
        focused_window = window_id(_attr(AS, focused, "AXWindow")) if focused is not None else None
        if focused_window is not None and focused_window not in on_screen:
            focused = None
        window_title = str(_attr(AS, root, "AXTitle") or app_name)

        refs: dict[str, Any] = {}
        elements: list[DesktopElement] = []
        visited: set[str] = set()
        window_bounds = _ax_bounds(AS, root)
        # Sheets, dialogs and popovers met by the walk; the hybrid backend isolates them.
        self.modal_roots: list[tuple[Any, Bounds | None, bool]] = []
        if self._cache is not None:
            cache = self._cache
            cache.begin(AS, root, window_bounds, lambda ref: cache.role_of(ref) or str(_attr(AS, ref, "AXRole")))
        self._walk(AS, root, elements, refs, visited, parent_id=None, depth=0, clip=window_bounds)
        # Inline editors may sit outside the main window's AX subtree. Falling
        # back to the whole app also exposes hundreds of inactive menu items.
        if focused is not None:
            self._walk(AS, focused, elements, refs, visited, parent_id=None, depth=0, clip=window_bounds)
        self._refs = refs
        logger.debug("ax observe app=%r elements=%d", app_name, len(elements))

        revision_payload = [
            {
                "id": e.id,
                "role": e.role,
                "name": e.name,
                "value": e.value,
                "enabled": e.enabled,
                "focused": e.focused,
                "selected": e.selected,
                "expanded": e.expanded,
                "parent_id": e.parent_id,
            }
            for e in elements
        ]
        revision = hashlib.sha256(json.dumps(revision_payload, sort_keys=True, default=str).encode()).hexdigest()
        return DesktopSnapshot(
            application=app_name,
            window=window_title,
            revision=revision,
            elements=tuple(elements),
            context={"pid": pid, "backend": "macos_ax"},
            captured_at_ms=round(time.time() * 1000),
        )

    def is_fresh(self, snapshot: DesktopSnapshot, action: ExecutableAction) -> bool:
        self.app.check_running()
        if snapshot.context.get("pid") != self.app.pid:
            return False
        if action.target_id:
            ref = self._refs.get(action.target_id)
            if ref is None:
                return False

            try:
                expected = snapshot.element(action.target_id)
            except KeyError:
                return False

            current = self._element_from_ref(
                ref,
                action.target_id,
                parent_id=expected.parent_id,
            )

            if current is None or current.semantic_guard() != action.target_guard:
                return False
        if action.secondary_target_id:
            # v0 AX backend does not expose semantic drag destinations.
            return False
        return True

    def execute(self, snapshot: DesktopSnapshot, action: ExecutableAction) -> None:
        if not self.is_fresh(snapshot, action):
            raise StaleDesktopState("macOS accessibility target changed before execution")

        if action.kind == ActionKind.WAIT:
            time.sleep(0.1)
            return
        # Accessibility actions can activate some apps too; the scope hands the front back.
        with self.app.input_scope():
            self._execute(action)

    def _execute(self, action: ExecutableAction) -> None:
        AS, _ = _frameworks()
        if action.kind == ActionKind.PRESS_KEY:
            _press_key(self.app, action.key or "")
            return
        if action.kind == ActionKind.HOTKEY:
            _press_hotkey(self.app, action.hotkey or "")
            return
        if action.kind == ActionKind.SCROLL:
            self.app.scroll(action.scroll_direction or "DOWN")
            return

        if not action.target_id:
            raise UnsupportedDesktopAction(f"{action.kind.value} requires a target on macOS AX")
        ref = self._refs.get(action.target_id)
        if ref is None:
            raise StaleDesktopState("Target no longer exists")

        if action.kind == ActionKind.CLICK:
            if action.click_modifier:
                # AXPress ignores modifiers; a modified click must be a real mouse event.
                bounds = _ax_bounds(AS, ref)
                if bounds is None:
                    raise UnsupportedDesktopAction("Modified click requires resolvable screen position")
                self._click_at(bounds, count=1, button="left", flags=modifier_flags((action.click_modifier,)))
                return
            actions = _action_names(AS, ref)
            if "AXPress" in actions:
                error = AS.AXUIElementPerformAction(ref, "AXPress")
                if error != 0:
                    raise UnsupportedDesktopAction(f"AX action AXPress failed with error {error}")
                return
            bounds = _ax_bounds(AS, ref)
            if bounds is None:
                raise UnsupportedDesktopAction("Click requires AXPress or resolvable screen position")
            self._click_at(bounds, count=1, button="left")
            return

        if action.kind in {ActionKind.DOUBLE_CLICK, ActionKind.RIGHT_CLICK}:
            bounds = _ax_bounds(AS, ref)
            if bounds is None:
                raise UnsupportedDesktopAction(f"{action.kind.value} requires resolvable screen position")
            self._click_at(
                bounds,
                count=2 if action.kind == ActionKind.DOUBLE_CLICK else 1,
                button="right" if action.kind == ActionKind.RIGHT_CLICK else "left",
            )
            return

        if action.kind in {ActionKind.TYPE_TEXT, ActionKind.SET_VALUE}:
            error, settable = AS.AXUIElementIsAttributeSettable(
                ref,
                "AXValue",
                None,
            )

            if error != 0 or not settable:
                raise UnsupportedDesktopAction(
                    "AXValue is not settable on this target"
                )
            if _attr(AS, ref, "AXRole") in _TEXT_ROLES and not _is_text_editor(AS, ref):
                raise UnsupportedDesktopAction("Text target is a label; activate its editor before entering a value")

            if action.value is None:
                raise UnsupportedDesktopAction(
                    f"{action.kind.value} requires an agent-supplied value"
                )

            if action.kind == ActionKind.TYPE_TEXT:
                value_to_set = str(action.value)
            else:
                current_value = _attr(AS, ref, "AXValue")
                value_to_set = _coerce_settable_ax_value(
                    current_value,
                    action.value,
                )

            error = AS.AXUIElementSetAttributeValue(
                ref,
                "AXValue",
                value_to_set,
            )

            if error != 0:
                raise UnsupportedDesktopAction(
                    f"Setting AXValue failed with error {error}"
                )

            return

        raise UnsupportedDesktopAction(f"MacOSAXBackend v0 cannot execute {action.kind.value}")

    def _walk(
        self,
        AS: Any,
        ref: Any,
        elements: list[DesktopElement],
        refs: dict[str, Any],
        visited: set[str],
        *,
        parent_id: str | None,
        depth: int,
        clip: Bounds | None = None,
        refresh: bool = False,
        row: RowState | None = None,
    ) -> None:
        """Walk what is on screen: `clip` is the visible area of the enclosing window
        and scroll areas. Elements with area outside it are skipped with their subtree;
        zero-size wrappers are not, since web content can overflow them."""
        if depth > self.max_depth or len(elements) >= self.max_elements:
            return
        element_id = self.identity_for(ref)
        if element_id in visited:
            return
        visited.add(element_id)

        node, refresh = self._node(AS, ref, element_id, parent_id, refresh, row)
        if clip is not None:
            bounds = node.bounds
            if depth > 0 and bounds is not None and bounds.width > 0 and bounds.height > 0 \
                    and node.role not in _UNCLIPPED_ROLES and not _overlaps(bounds, clip):
                return
            if depth == 0 or node.role in _CLIPPING_ROLES:
                clip = _intersect(clip, bounds)
        if node.modal and hasattr(self, "modal_roots"):
            self.modal_roots.append((ref, node.bounds, depth == 0))
        element = node.element
        next_parent = parent_id
        if element is not None:
            elements.append(element)
            refs[element.id] = ref
            next_parent = element.id

        child_row = row
        if node.role in _ROW_ROLES:
            # A list row carries the selection; the text and icons inside it show it.
            selected = (node.attributes or {}).get("AXSelected")
            child_row = RowState(selected=bool(selected) if selected is not None else (row and row.selected))
        for child in node.children:
            if len(elements) >= self.max_elements:
                break
            self._walk(AS, child, elements, refs, visited, parent_id=next_parent, depth=depth + 1,
                       clip=clip, refresh=refresh, row=child_row)

    def _node(
        self, AS: Any, ref: Any, element_id: str, parent_id: str | None, refresh: bool, row: RowState | None = None,
    ) -> tuple[AXNode, bool]:
        """The element's node, from the cache when it is unchanged; and whether its
        subtree must be read again."""
        cache = self._cache
        if cache is not None and not refresh:
            # Re-fetch the children (attributes=None) to compare with the cached list.
            node, refresh = cache.lookup(ref, lambda cached: _children(AS, ref, cached.role, None))
            if node is not None:
                if node.element is not None and node.element.parent_id != parent_id:
                    node.element = replace(node.element, parent_id=parent_id)
                return node, False
        node = self._read_node(AS, ref, element_id, parent_id, row)
        if cache is not None:
            cache.store(ref, node)
        return node, refresh

    def _read_node(
        self, AS: Any, ref: Any, element_id: str, parent_id: str | None, row: RowState | None = None,
    ) -> AXNode:
        attributes = copy_attributes(AS, ref, _ELEMENT_ATTRIBUTES)
        role = str(attributes.get("AXRole") or "") if attributes is not None else ""
        bounds = _bounds_from_values(AS, attributes.get("AXPosition"), attributes.get("AXSize")) \
            if attributes is not None else _ax_bounds(AS, ref)
        element = self._element_from_ref(ref, element_id, parent_id=parent_id, attributes=attributes, row=row)
        children = _children(AS, ref, role, attributes)
        return AXNode(attributes, role, bounds, element, children, is_modal(role, attributes))

    def _request_full_tree(self, AS: Any, app_ref: Any) -> None:
        """Ask Electron/Chromium apps to expose their full tree. They build it only for
        assistive clients that ask, and drop it again later, so this runs per observation."""
        if not self._full_tree_supported:
            return
        try:
            error = AS.AXUIElementSetAttributeValue(app_ref, "AXManualAccessibility", True)
        except Exception:
            error = -1
        if error == _AX_ATTRIBUTE_UNSUPPORTED:
            self._full_tree_supported = False

    def _element_from_ref(
        self,
        ref: Any,
        element_id: str,
        parent_id: str | None,
        attributes: dict[str, Any] | None = None,
        row: RowState | None = None,
    ) -> DesktopElement | None:
        AS, _ = _frameworks()
        if attributes is None:
            attributes = copy_attributes(AS, ref, _ELEMENT_ATTRIBUTES)
        batched = attributes is not None
        if not batched:  # batch read unsupported: one IPC call per attribute
            attributes = {name: _attr(AS, ref, name) for name in _ELEMENT_ATTRIBUTES}
        get = attributes.get
        role = get("AXRole")
        if not role:
            return None
        role = str(role)
        title = get("AXTitle")
        description = get("AXDescription")
        label = get("AXLabel")
        help_text = get("AXHelp")
        name = next((str(v) for v in (title, label, description, help_text) if v not in (None, "")), "")
        raw_value = get("AXValue")
        value = _coerce_value(raw_value)
        enabled = get("AXEnabled")
        focused = get("AXFocused")
        selected = get("AXSelected")
        if selected is None and row is not None and row.selected is not None:
            selected = row.selected
        expanded = get("AXExpanded")
        identifier = get("AXIdentifier")
        action_names = _action_names(AS, ref)
        bounds = _bounds_from_values(AS, get("AXPosition"), get("AXSize")) if batched else _ax_bounds(AS, ref)

        capabilities: list[ActionKind] = []
        # Text in a list, table or outline row has no press action, but a click on it
        # selects the row (a sidebar item, a search result).
        row_text = row is not None and role in _ROW_CONTENT_ROLES
        if "AXPress" in action_names or (
            bounds is not None and (role in _TEXT_ROLES or "AXOpen" in action_names or row_text)
        ):
            capabilities.append(ActionKind.CLICK)
        if "AXOpen" in action_names and bounds is not None:
            capabilities.append(ActionKind.DOUBLE_CLICK)
        if "AXShowMenu" in action_names and bounds is not None:
            capabilities.append(ActionKind.RIGHT_CLICK)
        settable = False
        # An element without a value has no settable value; skip that round trip.
        if raw_value is not None or not batched:
            try:
                error, settable = AS.AXUIElementIsAttributeSettable(ref, "AXValue", None)
                settable = error == 0 and bool(settable)
            except Exception:
                settable = False
        text_editor = role in _TEXT_ROLES and _is_text_editor(AS, ref)
        if settable and text_editor:
            capabilities.append(ActionKind.TYPE_TEXT)

        # Capability comes from AX itself, not a hard-coded role allowlist.
        if settable and (role not in _TEXT_ROLES or text_editor):
            capabilities.append(ActionKind.SET_VALUE)

        # Ignore anonymous containers with no useful action/state. Their children are
        # still traversed; this keeps the model-visible snapshot much smaller.
        structural_roles = {"AXWindow", "AXGroup", "AXToolbar", "AXMenu"}
        semantic = bool(
            name or value not in (None, "") or capabilities or role in structural_roles
        )
        if not semantic:
            return None

        metadata: dict[str, Any] = {}

        value_kind = _ax_value_kind(raw_value)
        if value_kind:
            metadata["value_type"] = value_kind

        if settable:
            metadata["ax_value_settable"] = True
        if role in _TEXT_ROLES:
            metadata["text_editable"] = text_editor

        if identifier:
            metadata["identifier"] = str(identifier)
        subrole = get("AXSubrole")
        if subrole is not None and str(subrole) in WINDOW_CONTROL_SUBROLES:
            metadata["window_control"] = str(subrole).removeprefix("AX").removesuffix("Button").lower()
        url = get("AXURL")
        if url is not None:
            # File-reference URLs are opaque IDs; NSURL can resolve the path
            # represented by that same accessible item without reading its data.
            if hasattr(url, "isFileURL") and url.isFileURL():
                url = url.filePathURL() or url
            metadata["url"] = str(url)
        if help_text and str(help_text) != name:
            metadata["help"] = str(help_text)[:300]

        return DesktopElement(
            id=element_id,
            role=role.removeprefix("AX"),
            name=name,
            value=value,
            actions=tuple(dict.fromkeys(capabilities)),
            enabled=True if enabled is None else bool(enabled),
            visible=True,
            focused=bool(focused),
            selected=None if selected is None else bool(selected),
            expanded=None if expanded is None else bool(expanded),
            parent_id=parent_id,
            bounds=bounds,
            source="macos_ax",
            metadata=metadata,
        )

    def _click_at(self, bounds: Bounds, *, count: int, button: str, flags: int = 0) -> None:
        self.app.click(bounds, count=count, right=button == "right", flags=flags)

    @staticmethod
    def _require_accessibility() -> None:
        AS, _ = _frameworks()
        if not AS.AXIsProcessTrusted():
            raise PermissionError(
                "macOS Accessibility permission is required. Grant it to your terminal/Python host in "
                "System Settings > Privacy & Security > Accessibility."
            )


def _frameworks() -> tuple[Any, Any]:
    try:
        import AppKit  # type: ignore
        import ApplicationServices as AS  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Install the macOS extra: pip install 'arc-cua[macos]'"
        ) from exc
    return AS, AppKit


# Lists, tables and outlines report which rows are on screen; web areas do not.
_LIST_ROLES = {"AXTable", "AXOutline", "AXList", "AXBrowser", "AXGrid"}
# Elements whose descendants are clipped to their bounds.
_CLIPPING_ROLES = {"AXScrollArea", "AXWebArea"}
_AX_ATTRIBUTE_UNSUPPORTED = -25205


def _children(AS: Any, ref: Any, role: str, attributes: dict[str, Any] | None) -> list[Any]:
    """The children the walk follows: visible rows for lists, otherwise AXChildren,
    fetched again when no attributes are given."""
    children = _visible_rows(AS, ref) if role in _LIST_ROLES else None
    if children is None:
        children = attributes.get("AXChildren") if attributes is not None else _attr(AS, ref, "AXChildren")
    try:
        return list(children or [])
    except TypeError:
        return []


def _visible_rows(AS: Any, ref: Any) -> list[Any] | None:
    """A list's header and on-screen rows instead of every row it holds; None if unknown.

    Columns are skipped: they contain the same cells as the rows."""
    values = copy_attributes(AS, ref, ("AXVisibleRows", "AXVisibleChildren", "AXHeader"))
    if values is None:
        return None
    rows = values["AXVisibleRows"]
    if rows is not None:
        header = values["AXHeader"]
        return ([header] if header is not None else []) + list(rows)
    visible = values["AXVisibleChildren"]
    return list(visible) if visible is not None else None


def _overlaps(bounds: Bounds, clip: Bounds) -> bool:
    return (
        bounds.x < clip.x + clip.width and bounds.x + bounds.width > clip.x
        and bounds.y < clip.y + clip.height and bounds.y + bounds.height > clip.y
    )


def _intersect(clip: Bounds | None, bounds: Bounds | None) -> Bounds | None:
    if clip is None or bounds is None:
        return clip or bounds
    left, top = max(clip.x, bounds.x), max(clip.y, bounds.y)
    right = min(clip.x + clip.width, bounds.x + bounds.width)
    bottom = min(clip.y + clip.height, bounds.y + bounds.height)
    return Bounds(left, top, max(0.0, right - left), max(0.0, bottom - top))


_ELEMENT_ATTRIBUTES = (
    "AXRole", "AXTitle", "AXDescription", "AXLabel", "AXHelp", "AXValue", "AXEnabled", "AXFocused",
    "AXSelected", "AXExpanded", "AXIdentifier", "AXURL", "AXPosition", "AXSize", "AXChildren", "AXSubrole",
    "AXModal",
)

# Elements that block the rest of their window (sheets, dialogs, popovers), and the
# window subroles of app-wide dialogs and alerts.
MODAL_ROLES = {"AXSheet", "AXDialog", "AXPopover"}
DIALOG_SUBROLES = {"AXDialog", "AXSystemDialog"}
# Never skipped as off-screen: popovers and menus can extend past their window.
_UNCLIPPED_ROLES = MODAL_ROLES | {"AXMenu"}


def is_modal(role: str, attributes: dict[str, Any] | None) -> bool:
    if role in MODAL_ROLES:
        return True
    if attributes is None:
        return False
    subrole = attributes.get("AXSubrole")
    return (subrole is not None and str(subrole) in DIALOG_SUBROLES) or attributes.get("AXModal") is True

# Title-bar buttons: part of the window, not of the application's content.
WINDOW_CONTROL_SUBROLES = {"AXCloseButton", "AXMinimizeButton", "AXZoomButton", "AXFullScreenButton"}


def copy_attributes(AS: Any, ref: Any, names: tuple[str, ...]) -> dict[str, Any] | None:
    """Read several attributes in one cross-process call; None if unsupported.

    Each AX attribute read is an IPC round trip to the target app, so reading
    them one at a time dominated observation. Missing attributes come back as
    AXValue error placeholders and are mapped to None.
    """
    try:
        error, values = AS.AXUIElementCopyMultipleAttributeValues(ref, list(names), 0, None)
    except Exception:
        return None
    if error != 0 or values is None or len(values) != len(names):
        return None
    CF = _core_foundation()
    ax_value_type = AS.AXValueGetTypeID()
    result: dict[str, Any] = {}
    for name, value in zip(names, values):
        if (
            value is not None
            and CF.CFGetTypeID(value) == ax_value_type
            and AS.AXValueGetType(value) == AS.kAXValueAXErrorType
        ):
            value = None
        result[name] = value
    return result


def _attr(AS: Any, ref: Any, name: str) -> Any:
    try:
        error, value = AS.AXUIElementCopyAttributeValue(ref, name, None)
    except Exception:
        return None
    return value if error == 0 else None


def _action_names(AS: Any, ref: Any) -> set[str]:
    try:
        error, names = AS.AXUIElementCopyActionNames(ref, None)
    except Exception:
        return set()
    if error != 0 or not names:
        return set()
    return {str(name) for name in names}


def _is_text_editor(AS: Any, ref: Any) -> bool:
    # Item labels can claim a writable AXValue without committing edits to the
    # underlying item. Require focus or text-selection support as well.
    if _attr(AS, ref, "AXFocused"):
        return True
    for attribute in ("AXFocused", "AXSelectedTextRange"):
        try:
            error, settable = AS.AXUIElementIsAttributeSettable(ref, attribute, None)
            if error == 0 and settable:
                return True
        except Exception:
            continue
    return False


class _AXIdentityRegistry:
    """Identify remote AX objects, not their temporary Python/CF wrappers.

    Keep the previous observation's references until the next traversal finishes.
    CFEqual resolves hash collisions; IDs are never recycled within a session.
    """

    def __init__(self) -> None:
        self._buckets: dict[int, list[tuple[Any, str]]] = {}
        self._seen: set[str] = set()
        self._next_id = 0

    def begin_observation(self) -> None:
        self._buckets = {
            key: retained for key, entries in self._buckets.items()
            if (retained := [(ref, identity) for ref, identity in entries if identity in self._seen])
        }
        self._seen.clear()

    def id_for(self, ref: Any) -> str:
        CF = _core_foundation()
        entries = self._buckets.setdefault(int(CF.CFHash(ref)), [])
        for previous, identity in entries:
            if CF.CFEqual(previous, ref):
                self._seen.add(identity)
                return identity
        self._next_id += 1
        identity = f"ax_{self._next_id}"
        entries.append((ref, identity))
        self._seen.add(identity)
        return identity


def _core_foundation() -> Any:
    import CoreFoundation  # type: ignore

    return CoreFoundation


def _ax_value_kind(
    value: Any,
) -> str | None:
    if value is None:
        return None

    # Plain types first: a missed attribute lookup on a PyObjC object is slow.
    if isinstance(value, bool):
        return "boolean"

    if isinstance(value, int):
        return "integer"

    if isinstance(value, float):
        return "number"

    if isinstance(value, str):
        return "text"

    if hasattr(value, "timeIntervalSince1970"):
        return "date_time"

    return type(value).__name__


def _parse_time_literal(
    value: str,
) -> tuple[int, int] | None:
    text = value.strip().lower()

    match = re.fullmatch(
        r"(\d{1,2})(?::(\d{1,2}))?\s*([ap])?\.?m?\.?",
        text,
    )

    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    suffix = match.group(3)

    if minute > 59:
        return None

    if suffix is not None:
        if not 1 <= hour <= 12:
            return None

        hour = hour % 12

        if suffix == "p":
            hour += 12

    elif not 0 <= hour <= 23:
        return None

    return hour, minute


def _coerce_settable_ax_value(
    current_value: Any,
    supplied_value: Any,
) -> Any:
    # NSDate / CFDate-like values.
    if current_value is not None and hasattr(
        current_value,
        "timeIntervalSince1970",
    ):
        if not isinstance(supplied_value, str):
            raise UnsupportedDesktopAction(
                "Date/time AXValue requires a string input"
            )

        parsed_time = _parse_time_literal(supplied_value)

        try:
            timestamp = float(
                current_value.timeIntervalSince1970()
            )
        except Exception as exc:
            raise UnsupportedDesktopAction(
                "Could not read current date/time AXValue"
            ) from exc

        current_datetime = datetime.fromtimestamp(
            timestamp
        ).astimezone()

        if parsed_time is not None:
            hour, minute = parsed_time
            target_datetime = current_datetime.replace(
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0,
            )
        else:
            try:
                target_datetime = datetime.fromisoformat(
                    supplied_value
                )
            except ValueError as exc:
                raise UnsupportedDesktopAction(
                    f"Could not parse date/time value: {supplied_value!r}"
                ) from exc

            if target_datetime.tzinfo is None:
                target_datetime = target_datetime.replace(
                    tzinfo=current_datetime.tzinfo
                )

        try:
            import Foundation  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "Foundation is required for macOS date/time AX values. "
                "Install: pip install 'arc-cua[macos]'"
            ) from exc

        return Foundation.NSDate.dateWithTimeIntervalSince1970_(
            target_datetime.timestamp()
        )

    if isinstance(current_value, bool):
        if isinstance(supplied_value, str):
            normalized = supplied_value.strip().lower()

            if normalized in {
                "true",
                "yes",
                "on",
                "1",
                "enabled",
            }:
                return True

            if normalized in {
                "false",
                "no",
                "off",
                "0",
                "disabled",
            }:
                return False

            raise UnsupportedDesktopAction(
                f"Could not parse boolean value: {supplied_value!r}"
            )

        return bool(supplied_value)

    if isinstance(current_value, int) and not isinstance(
        current_value,
        bool,
    ):
        try:
            return int(supplied_value)
        except (TypeError, ValueError) as exc:
            raise UnsupportedDesktopAction(
                f"Could not parse integer value: {supplied_value!r}"
            ) from exc

    if isinstance(current_value, float):
        try:
            return float(supplied_value)
        except (TypeError, ValueError) as exc:
            raise UnsupportedDesktopAction(
                f"Could not parse numeric value: {supplied_value!r}"
            ) from exc

    if isinstance(current_value, str):
        return str(supplied_value)

    return supplied_value


def _coerce_value(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # AXValue/attributed objects are not useful to the policy as opaque Python refs.
    text = str(value)
    return text if len(text) <= 300 and not text.startswith("<AX") else None


def _quartz() -> Any:
    try:
        import Quartz  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Install the macOS extra: pip install 'arc-cua[macos]'") from exc
    return Quartz


# Physical key positions from Apple's HIToolbox Events.h (kVK_ANSI_* and kVK_*).
# Supporting a key here does not add it to the policy's default action space.
_KEYCODES = {
    "ENTER": 36,
    "ESCAPE": 53,
    "TAB": 48,
    "SPACE": 49,
    "BACKSPACE": 51,
    "DELETE": 117,
    "ARROW_LEFT": 123,
    "ARROW_RIGHT": 124,
    "ARROW_DOWN": 125,
    "ARROW_UP": 126,
    "HOME": 115,
    "END": 119,
    "PAGE_UP": 116,
    "PAGE_DOWN": 121,
    "A": 0,
    "B": 11,
    "C": 8,
    "D": 2,
    "E": 14,
    "F": 3,
    "G": 5,
    "H": 4,
    "I": 34,
    "J": 38,
    "K": 40,
    "L": 37,
    "M": 46,
    "N": 45,
    "O": 31,
    "P": 35,
    "Q": 12,
    "R": 15,
    "S": 1,
    "T": 17,
    "U": 32,
    "V": 9,
    "W": 13,
    "X": 7,
    "Y": 16,
    "Z": 6,
    "0": 29,
    "1": 18,
    "2": 19,
    "3": 20,
    "4": 21,
    "5": 23,
    "6": 22,
    "7": 26,
    "8": 28,
    "9": 25,
    "MINUS": 27,
    "EQUAL": 24,
    "LEFT_BRACKET": 33,
    "RIGHT_BRACKET": 30,
    "BACKSLASH": 42,
    "SEMICOLON": 41,
    "QUOTE": 39,
    "COMMA": 43,
    "PERIOD": 47,
    "SLASH": 44,
    "GRAVE": 50,
    "F1": 122,
    "F2": 120,
    "F3": 99,
    "F4": 118,
    "F5": 96,
    "F6": 97,
    "F7": 98,
    "F8": 100,
    "F9": 101,
    "F10": 109,
    "F11": 103,
    "F12": 111,
    "F13": 105,
    "F14": 107,
    "F15": 113,
    "F16": 106,
    "F17": 64,
    "F18": 79,
    "F19": 80,
    "F20": 90,
}


def _press_key(app: MacOSApp, key: str) -> None:
    code = _KEYCODES.get(key)
    if code is None:
        raise UnsupportedDesktopAction(f"Unsupported macOS key: {key}")
    app.press(code)


def _press_hotkey(app: MacOSApp, hotkey: str) -> None:
    try:
        modifiers, key = parse_hotkey(hotkey)
    except ValueError as exc:
        raise UnsupportedDesktopAction(str(exc)) from exc
    code = _KEYCODES.get(key)
    if code is None:
        raise UnsupportedDesktopAction(f"Unsupported macOS hotkey key: {key}")
    app.shortcut(modifiers, key, code, modifier_flags(modifiers))


def modifier_flags(modifiers) -> int:
    """Quartz event flags for arc-cua modifier names (MOD is Command on macOS)."""
    Q = _quartz()
    masks = {
        "MOD": Q.kCGEventFlagMaskCommand,
        "SHIFT": Q.kCGEventFlagMaskShift,
        "ALT": Q.kCGEventFlagMaskAlternate,
        "CTRL": Q.kCGEventFlagMaskControl,
    }
    flags = 0
    for modifier in modifiers:
        if modifier not in masks:
            raise UnsupportedDesktopAction(f"Unsupported macOS modifier: {modifier}")
        flags |= masks[modifier]
    return flags


def _ax_bounds(AS: Any, ref: Any) -> Bounds | None:
    return _bounds_from_values(AS, _attr(AS, ref, "AXPosition"), _attr(AS, ref, "AXSize"))


def _bounds_from_values(AS: Any, pos: Any, size: Any) -> Bounds | None:
    if pos is None or size is None:
        return None
    try:
        position_ok, point = AS.AXValueGetValue(pos, AS.kAXValueCGPointType, None)
        size_ok, dimensions = AS.AXValueGetValue(size, AS.kAXValueCGSizeType, None)
        if not position_ok or not size_ok:
            return None
        x = float(point.x)
        y = float(point.y)
        w = float(dimensions.width)
        h = float(dimensions.height)
    except (AttributeError, TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return Bounds(x=x, y=y, width=w, height=h)
