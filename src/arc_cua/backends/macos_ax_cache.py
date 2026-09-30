"""Opt-in cache for the macOS accessibility walk.

Reading an element costs a request the target app answers on its main thread, so a
full walk of an ordinary window takes tens of milliseconds even when nothing has
changed. The cache keeps each element from the previous observation and re-reads
only what the app reports as changed (AX notifications), plus a light structure
check: each container's children are re-fetched and compared, which catches
elements added without a notification. Values an app changes without notifying
(for example a clock) can be stale until that element is read again; actions are
unaffected because targets are re-checked before they run.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from ..models import Bounds, DesktopElement

logger = logging.getLogger(__name__)

# Element-level changes: re-read the element.
_ELEMENT_CHANGES = {
    "AXValueChanged", "AXTitleChanged", "AXSelectedChildrenChanged", "AXSelectedRowsChanged",
    "AXSelectedTextChanged", "AXSelectedCellsChanged", "AXRowCountChanged", "AXRowExpanded",
    "AXRowCollapsed", "AXFocusedUIElementChanged", "AXElementBusyChanged",
}
# Geometry or content changes: re-read the element's whole subtree.
_SUBTREE_CHANGES = {"AXLayoutChanged", "AXResized", "AXMoved", "AXScrollPositionChanged"}
# Window-level changes: start over.
_WINDOW_CHANGES = {
    "AXFocusedWindowChanged", "AXMainWindowChanged", "AXWindowCreated", "AXWindowMoved", "AXWindowResized",
    "AXWindowMiniaturized", "AXWindowDeminiaturized", "AXMenuOpened", "AXMenuClosed",
}
NOTIFICATIONS = sorted(_ELEMENT_CHANGES | _SUBTREE_CHANGES | _WINDOW_CHANGES | {"AXCreated", "AXUIElementDestroyed"})

# Roles whose children are compared on every observation. Other leaves (text, images,
# buttons, fields) rarely gain children; these host loaded content.
HOST_ROLES = {
    "AXGroup", "AXCell", "AXRow", "AXScrollArea", "AXWebArea", "AXSplitGroup", "AXLayoutArea",
    "AXSheet", "AXPopover", "AXTabGroup", "AXList", "AXOutline", "AXTable", "AXBrowser", "AXGrid",
}


@dataclass
class AXNode:
    """One element as read from the app, with the children the walk follows."""

    attributes: dict[str, Any] | None
    role: str
    bounds: Bounds | None
    element: DesktopElement | None
    children: list[Any] = field(default_factory=list)
    modal: bool = False  # a sheet, dialog or popover, or flagged AXModal


class AXChangeFeed:
    """Collects (element, notification) pairs for one app on a background run loop."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._queue: deque[tuple[Any, str]] = deque()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self.available = False
        self._thread = threading.Thread(target=self._run, name="arc-cua-ax-cache", daemon=True)
        self._thread.start()
        self._ready.wait(2)

    def _run(self) -> None:
        try:
            import ApplicationServices as AS
            import objc
            from CoreFoundation import (
                CFRunLoopAddSource,
                CFRunLoopGetCurrent,
                CFRunLoopRunInMode,
                kCFRunLoopDefaultMode,
            )

            @objc.callbackFor(AS.AXObserverCreate)
            def callback(observer: Any, element: Any, notification: Any, refcon: Any) -> None:
                self._queue.append((element, str(notification)))

            error, observer = AS.AXObserverCreate(self.pid, callback, None)
            if error != 0 or observer is None:
                logger.debug("AX cache feed unavailable pid=%s error=%s", self.pid, error)
                return
            app = AS.AXUIElementCreateApplication(self.pid)
            for name in NOTIFICATIONS:
                AS.AXObserverAddNotification(observer, app, name, None)
            CFRunLoopAddSource(CFRunLoopGetCurrent(), AS.AXObserverGetRunLoopSource(observer), kCFRunLoopDefaultMode)
            self.available = True
        finally:
            self._ready.set()
        while not self._stop.is_set():
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.05, False)

    def drain(self) -> list[tuple[Any, str]]:
        items = []
        while self._queue:
            items.append(self._queue.popleft())
        return items

    def close(self) -> None:
        self._stop.set()


class AXNodeCache:
    """Elements from the previous observation, and what must be read again."""

    def __init__(self, feed: AXChangeFeed) -> None:
        self.feed = feed
        self.root: Any = None
        self.root_bounds: Bounds | None = None
        self.nodes: dict[Any, AXNode] = {}  # AX element references compare with CFEqual
        self.parents: dict[Any, Any] = {}
        self._dirty: set[Any] = set()
        self._subtrees: set[Any] = set()

    def begin(self, AS: Any, root: Any, root_bounds: Bounds | None, role_of: Callable[[Any], str]) -> None:
        """Apply the changes reported since the last observation. A different window,
        or the same window moved or resized, starts over."""
        changes = self.feed.drain()
        if not self.feed.available or root != self.root or root_bounds != self.root_bounds:
            self.reset(root, root_bounds)
            return
        dirty, subtrees = {root}, set()  # the window itself is always re-read (title, focus)
        for element, name in changes:
            if name in _WINDOW_CHANGES:
                self.reset(root, root_bounds)
                return
            if name in _SUBTREE_CHANGES:
                subtrees.add(element)
            elif name == "AXCreated" or name == "AXUIElementDestroyed":
                parent = self.parents.get(element) or _parent(AS, element)
                if parent is None:
                    self.reset(root, root_bounds)
                    return
                dirty.add(parent)
                self.nodes.pop(element, None)
            elif name == "AXValueChanged" and role_of(element) == "AXScrollBar":
                # Scrolling is announced only as the scroll bar's value; the content
                # it scrolls, the enclosing scroll area, has changed.
                parent = self.parents.get(element) or _parent(AS, element)
                if parent is None:
                    self.reset(root, root_bounds)
                    return
                subtrees.add(parent)
            else:
                dirty.add(element)
                if name == "AXFocusedUIElementChanged":
                    dirty.update(ref for ref, node in self.nodes.items() if node.element and node.element.focused)
        self._dirty, self._subtrees = dirty, subtrees

    def reset(self, root: Any, root_bounds: Bounds | None) -> None:
        self.root, self.root_bounds = root, root_bounds
        self.nodes.clear()
        self.parents.clear()
        self._dirty, self._subtrees = set(), set()

    def lookup(self, ref: Any, current_children: Callable[[AXNode], list[Any]]) -> tuple[AXNode | None, bool]:
        """The cached node, or None when it must be read again; and whether its
        subtree must be read again."""
        if ref in self._subtrees:
            return None, True
        node = None if ref in self._dirty else self.nodes.get(ref)
        if node is None:
            return None, False
        if node.children or node.role in HOST_ROLES:
            if current_children(node) != node.children:
                return None, True
        return node, False

    def store(self, ref: Any, node: AXNode) -> None:
        self.nodes[ref] = node
        for child in node.children:
            self.parents[child] = ref

    def role_of(self, ref: Any) -> str | None:
        node = self.nodes.get(ref)
        return node.role if node is not None else None

    def close(self) -> None:
        self.feed.close()


def _parent(AS: Any, element: Any) -> Any:
    try:
        error, parent = AS.AXUIElementCopyAttributeValue(element, "AXParent", None)
    except Exception:
        return None
    return parent if error == 0 else None
