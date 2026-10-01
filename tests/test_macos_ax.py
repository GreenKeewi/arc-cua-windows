from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from arc_cua import ActionKind, Bounds, DesktopElement, DesktopSnapshot
from arc_cua.backends import macos_ax, macos_hybrid
from arc_cua.errors import TargetUnavailable, UnsupportedDesktopAction
from arc_cua.models import ExecutableAction


class Ref:
    def __init__(self, identity, **attributes):
        self.identity = identity
        self.attributes = attributes

    def __repr__(self):
        # Remote objects can have different wrappers, or reused wrapper addresses.
        return "<AXUIElement 0x123> {pid=1}"


def fake_app(**attributes):
    """Stands in for MacOSApp: a running app with one on-screen window."""
    defaults = dict(
        pid=123, name="Editor", check_running=lambda: None, input_scope=nullcontext,
        windows=lambda: [SimpleNamespace(window_id=1)],
    )
    return SimpleNamespace(**{**defaults, **attributes})


def install_cf(monkeypatch):
    monkeypatch.setattr(macos_ax, "_core_foundation", lambda: SimpleNamespace(
        CFHash=lambda ref: 7,  # Deliberately collide distinct remote objects.
        CFEqual=lambda a, b: a.identity == b.identity,
    ))


def test_identity_survives_wrapper_changes_and_hash_collisions(monkeypatch):
    install_cf(monkeypatch)
    ids = macos_ax._AXIdentityRegistry()
    ids.begin_observation()
    first = ids.id_for(Ref("first"))
    second = ids.id_for(Ref("second"))
    assert first != second
    ids.begin_observation()
    assert ids.id_for(Ref("first")) == first
    assert ids.id_for(Ref("second")) == second
    ids.begin_observation()
    ids.id_for(Ref("first"))
    ids.begin_observation()
    assert sum(len(bucket) for bucket in ids._buckets.values()) == 1
    assert ids.id_for(Ref("second")) not in {first, second}


def test_traversal_does_not_drop_siblings_with_reused_wrapper_addresses(monkeypatch):
    install_cf(monkeypatch)
    left, right = Ref("left"), Ref("right")
    root = Ref("root", AXChildren=[left, right, Ref("left")])
    backend = object.__new__(macos_ax.MacOSAXBackend)
    backend._cache = None
    backend.max_depth = 10
    backend.max_elements = 10
    backend._identities = macos_ax._AXIdentityRegistry()
    monkeypatch.setattr(macos_ax, "_attr", lambda api, ref, name: ref.attributes.get(name))
    backend._element_from_ref = lambda ref, identity, **kw: DesktopElement(
        id=identity, role="Group", name=ref.identity,
    )
    elements = []
    backend._walk(None, root, elements, {}, set(), parent_id=None, depth=0)
    assert [element.name for element in elements] == ["root", "left", "right"]


def modal_backend(monkeypatch, windows, modal_roots):
    install_cf(monkeypatch)
    monkeypatch.setattr(macos_hybrid, "_ax_framework", lambda: SimpleNamespace(
        AXUIElementCreateApplication=lambda pid: Ref("app", AXWindows=windows)))
    monkeypatch.setattr(macos_hybrid, "_ax_copy_attribute", lambda api, ref, name: ref.attributes.get(name))
    monkeypatch.setattr(macos_hybrid, "copy_attributes",
                        lambda api, ref, names: {name: ref.attributes.get(name) for name in names})
    monkeypatch.setattr(macos_hybrid, "_bounds_from_values", lambda api, pos, size: None)
    collected = []
    monkeypatch.setattr(macos_hybrid, "_collect_modal_ax_elements",
                        lambda backend, roots: (collected.extend(roots) or (), None))
    ids = macos_ax._AXIdentityRegistry()
    ids.begin_observation()
    return SimpleNamespace(modal_roots=modal_roots, identity_for=ids.id_for), collected


def test_observed_sheet_reuses_the_walked_elements(monkeypatch):
    backend, collected = modal_backend(monkeypatch, [Ref("doc", AXRole="AXWindow")],
                                       [(Ref("sheet"), Bounds(0, 0, 10, 10), True)])
    snapshot = DesktopSnapshot(application="App", window="Sheet", revision="1",
                               elements=(DesktopElement(id="ok", role="Button", name="OK"),))
    elements, bounds = macos_hybrid._modal_elements(backend, 1, snapshot)
    assert [e.id for e in elements] == ["ok"] and bounds == Bounds(0, 0, 10, 10)
    assert collected == []


def test_dialog_windows_count_but_tooltips_and_other_windows_sheets_do_not(monkeypatch):
    alert = Ref("alert", AXRole="AXWindow", AXSubrole="AXDialog")
    windows = [Ref("doc", AXRole="AXWindow", AXSubrole="AXStandardWindow"),
               Ref("tip", AXRole="AXHelpTag", AXModal=None), alert,
               Ref("alert", AXRole="AXWindow", AXSubrole="AXDialog")]
    backend, collected = modal_backend(monkeypatch, windows, [])
    macos_hybrid._modal_elements(backend, 1, DesktopSnapshot(application="A", window="W", revision="1", elements=()))
    assert [ref.identity for ref in collected] == ["alert"]  # once, although listed through two wrappers


def test_no_dialog_means_no_isolation(monkeypatch):
    backend, collected = modal_backend(monkeypatch, [Ref("doc", AXRole="AXWindow", AXSubrole="AXStandardWindow")], [])
    snapshot = DesktopSnapshot(application="A", window="W", revision="1", elements=())
    assert macos_hybrid._modal_elements(backend, 1, snapshot) == ((), None)


def test_inline_editor_uses_main_window_without_traversing_inactive_app_menus(monkeypatch):
    install_cf(monkeypatch)
    window = Ref("main", AXTitle="Document")
    editor = Ref("editor")
    app = Ref("app", AXMainWindow=window, AXFocusedUIElement=editor,
              AXChildren=[window, editor, Ref("inactive menus")])
    monkeypatch.setattr(macos_ax, "_frameworks", lambda: (SimpleNamespace(), None))
    monkeypatch.setattr(macos_ax, "_attr", lambda api, ref, name: ref.attributes.get(name))
    monkeypatch.setattr(macos_ax, "window_id", lambda ref: 1 if ref is window else None)
    monkeypatch.setattr(macos_ax.sys, "platform", "darwin")
    monkeypatch.setattr(macos_ax.MacOSAXBackend, "_require_accessibility", staticmethod(lambda: None))
    monkeypatch.setattr(macos_ax, "MacOSApp", lambda pid: fake_app(pid=pid, ax=app))
    backend = macos_ax.MacOSAXBackend(123)
    backend._element_from_ref = lambda ref, identity, **kw: DesktopElement(
        id=identity, role="Group", name=ref.identity,
    )
    snapshot = backend.observe()
    assert snapshot.window == "Document"
    assert [element.name for element in snapshot.elements] == ["main", "editor"]


def test_geometry_decodes_axvalue_boxes(monkeypatch):
    point, dimensions = object(), object()
    decoded = {point: SimpleNamespace(x=10, y=20), dimensions: SimpleNamespace(width=30, height=40)}
    api = SimpleNamespace(kAXValueCGPointType=1, kAXValueCGSizeType=2,
                          AXValueGetValue=lambda value, kind, out: (True, decoded[value]))
    monkeypatch.setattr(macos_ax, "_attr", lambda api, ref, name: {
        "AXPosition": point, "AXSize": dimensions,
    }.get(name))
    assert macos_ax._ax_bounds(api, object()) == Bounds(10, 20, 30, 40)


def test_context_menu_is_not_an_ordinary_click_capability(monkeypatch):
    api = SimpleNamespace(AXUIElementIsAttributeSettable=lambda *args: (0, False))
    monkeypatch.setattr(macos_ax, "_frameworks", lambda: (api, None))
    monkeypatch.setattr(macos_ax, "_attr", lambda api, ref, name: {"AXRole": "AXStaticText"}.get(name))
    monkeypatch.setattr(macos_ax, "_action_names", lambda *args: {"AXShowMenu"})
    monkeypatch.setattr(macos_ax, "_ax_bounds", lambda *args: Bounds(10, 20, 30, 40))
    backend = object.__new__(macos_ax.MacOSAXBackend)
    backend._cache = None
    element = backend._element_from_ref(object(), "target", None)
    assert ActionKind.CLICK not in element.actions
    assert element.actions == (ActionKind.RIGHT_CLICK,)


def test_click_does_not_dispatch_context_menu(monkeypatch):
    native, pointer = [], []
    api = SimpleNamespace(AXUIElementPerformAction=lambda ref, action: native.append(action) or 0)
    monkeypatch.setattr(macos_ax, "_frameworks", lambda: (api, None))
    monkeypatch.setattr(macos_ax, "_action_names", lambda *args: {"AXOpen", "AXShowMenu"})
    bounds = Bounds(10, 20, 30, 40)
    monkeypatch.setattr(macos_ax, "_ax_bounds", lambda *args: bounds)
    backend = object.__new__(macos_ax.MacOSAXBackend)
    backend._cache = None
    backend.app = fake_app()
    backend._click_at = lambda bounds, **kw: pointer.append((bounds, kw))
    backend._refs = {"target": object()}
    backend.is_fresh = lambda *args: True
    snapshot = DesktopSnapshot(application="Files", window="Folder", revision="1", elements=())
    backend.execute(snapshot, ExecutableAction(kind=ActionKind.CLICK, target_id="target"))
    assert native == []
    assert pointer == [(bounds, {"count": 1, "button": "left"})]
    backend.execute(snapshot, ExecutableAction(kind=ActionKind.DOUBLE_CLICK, target_id="target"))
    assert native == []
    assert pointer[-1] == (bounds, {"count": 2, "button": "left"})


def test_opaque_ax_wrappers_do_not_change_observed_values():
    assert macos_ax._coerce_value(Ref("tab")) is None
    assert macos_ax._coerce_value("Document") == "Document"


def test_freshness_rejects_item_replaced_by_same_name_at_a_different_url():
    before = DesktopElement(id="item", role="TextField", value="report.pdf",
                            metadata={"url": "file:///first/report.pdf"})
    current = DesktopElement(id="item", role="TextField", value="report.pdf",
                             metadata={"url": "file:///second/report.pdf"})
    backend = object.__new__(macos_ax.MacOSAXBackend)
    backend._cache = None
    backend._refs = {"item": object()}
    backend.app = fake_app()
    backend._element_from_ref = lambda *args, **kwargs: current
    snapshot = DesktopSnapshot(application="Files", window="Folder", revision="1",
                               elements=(before,), context={"pid": 123})
    action = ExecutableAction(kind=ActionKind.CLICK, target_id="item", target_guard=before.semantic_guard())
    assert not backend.is_fresh(snapshot, action)


@pytest.mark.parametrize(("focused", "settable_attributes", "editable"), [
    (False, {"AXValue"}, False),  # An item label that only changes its display value.
    (True, {"AXValue", "AXFocused", "AXSelectedTextRange"}, True),
    (False, {"AXValue", "AXFocused", "AXSelectedTextRange"}, True),
])
def test_only_real_text_editors_offer_value_entry(monkeypatch, focused, settable_attributes, editable):
    writes = []
    api = SimpleNamespace(
        AXUIElementIsAttributeSettable=lambda ref, name, out: (0, name in settable_attributes),
        AXUIElementSetAttributeValue=lambda ref, name, value: writes.append((name, value)) or 0,
    )
    monkeypatch.setattr(macos_ax, "_frameworks", lambda: (api, None))
    monkeypatch.setattr(macos_ax, "_attr", lambda api, ref, name: {
        "AXRole": "AXTextField", "AXValue": "Original", "AXFocused": focused,
    }.get(name))
    monkeypatch.setattr(macos_ax, "_action_names", lambda *args: set())
    monkeypatch.setattr(macos_ax, "_ax_bounds", lambda *args: None)
    backend = object.__new__(macos_ax.MacOSAXBackend)
    backend._cache = None
    backend.app = fake_app()
    backend._refs = {"target": object()}
    backend.is_fresh = lambda *args: True
    element = backend._element_from_ref(backend._refs["target"], "target", None)
    assert (ActionKind.TYPE_TEXT in element.actions) is editable
    assert (ActionKind.SET_VALUE in element.actions) is editable
    snapshot = DesktopSnapshot(application="Files", window="Folder", revision="1", elements=(element,))
    action = ExecutableAction(kind=ActionKind.TYPE_TEXT, target_id="target", value="Changed")
    if editable:
        backend.execute(snapshot, action)
        assert writes == [("AXValue", "Changed")]
    else:
        with pytest.raises(UnsupportedDesktopAction, match="label"):
            backend.execute(snapshot, action)
        assert writes == []


def test_observe_uses_a_window_on_this_desktop_not_the_focused_one_elsewhere(monkeypatch):
    install_cf(monkeypatch)
    elsewhere, here = Ref("elsewhere", AXTitle="Other desktop"), Ref("here", AXTitle="This desktop")
    app = Ref("app", AXFocusedWindow=elsewhere, AXMainWindow=elsewhere, AXWindows=[elsewhere, here])
    monkeypatch.setattr(macos_ax, "_frameworks", lambda: (SimpleNamespace(), None))
    monkeypatch.setattr(macos_ax, "_attr", lambda api, ref, name: ref.attributes.get(name))
    ids = {"elsewhere": 7, "here": 1}
    monkeypatch.setattr(macos_ax, "window_id", lambda ref: ids.get(getattr(ref, "identity", None)))
    backend = object.__new__(macos_ax.MacOSAXBackend)
    backend._cache = None
    backend.app = fake_app(ax=app)
    backend.max_depth, backend.max_elements = 10, 10
    backend._identities = macos_ax._AXIdentityRegistry()
    backend._full_tree_supported = True
    backend._element_from_ref = lambda ref, identity, **kw: DesktopElement(
        id=identity, role="Window", name=ref.identity,
    )
    snapshot = backend.observe()
    assert snapshot.window == "This desktop"
    assert snapshot.context["pid"] == 123


def test_observe_waits_for_an_app_that_is_replacing_its_window(monkeypatch):
    install_cf(monkeypatch)
    window = Ref("window", AXTitle="Invoices")
    app = Ref("app", AXFocusedWindow=window, AXMainWindow=window, AXWindows=[window])
    monkeypatch.setattr(macos_ax, "_frameworks", lambda: (SimpleNamespace(), None))
    monkeypatch.setattr(macos_ax, "_attr", lambda api, ref, name: ref.attributes.get(name))
    monkeypatch.setattr(macos_ax, "window_id", lambda ref: 1)
    polls = iter([[], [], [SimpleNamespace(window_id=1)]])
    backend = object.__new__(macos_ax.MacOSAXBackend)
    backend._cache = None
    backend.app = fake_app(ax=app, windows=lambda: next(polls, [SimpleNamespace(window_id=1)]))
    backend.max_depth, backend.max_elements = 10, 10
    backend._identities = macos_ax._AXIdentityRegistry()
    backend._full_tree_supported = True
    backend._element_from_ref = lambda ref, identity, **kw: DesktopElement(id=identity, role="Window", name="")
    assert backend.observe().window == "Invoices"


def test_observe_fails_clearly_when_the_app_has_no_usable_window(monkeypatch):
    def no_window():
        raise TargetUnavailable("Editor has no open window on this desktop.")

    backend = object.__new__(macos_ax.MacOSAXBackend)
    backend._cache = None
    backend.app = fake_app(windows=lambda: [], require_window=no_window)
    monkeypatch.setattr(macos_ax, "_frameworks", lambda: (SimpleNamespace(), None))
    monkeypatch.setattr(macos_ax, "_WINDOW_WAIT_S", 0.1)
    with pytest.raises(TargetUnavailable, match="no open window"):
        backend.observe()


def test_freshness_fails_clearly_once_the_app_quits():
    def quit():
        raise TargetUnavailable("Editor (process ID 123) quit.")

    backend = object.__new__(macos_ax.MacOSAXBackend)
    backend._cache = None
    backend.app = fake_app(check_running=quit)
    snapshot = DesktopSnapshot(application="Editor", window="Doc", revision="1", elements=(), context={"pid": 123})
    with pytest.raises(TargetUnavailable, match="quit"):
        backend.is_fresh(snapshot, ExecutableAction(kind=ActionKind.PRESS_KEY, key="ENTER"))


def walk_tree(monkeypatch, root, *, clip):
    """Walk fake refs whose attributes include plain (x, y) / (w, h) geometry."""
    install_cf(monkeypatch)
    monkeypatch.setattr(macos_ax, "_attr", lambda api, ref, name: ref.attributes.get(name))
    monkeypatch.setattr(macos_ax, "copy_attributes",
                        lambda api, ref, names: {name: ref.attributes.get(name) for name in names})
    monkeypatch.setattr(macos_ax, "_bounds_from_values",
                        lambda api, pos, size: Bounds(*pos, *size) if pos and size else None)
    backend = object.__new__(macos_ax.MacOSAXBackend)
    backend._cache = None
    backend.max_depth, backend.max_elements = 10, 100
    backend._identities = macos_ax._AXIdentityRegistry()
    backend._element_from_ref = lambda ref, identity, **kw: DesktopElement(id=identity, role="Group", name=ref.identity)
    elements = []
    backend._walk(None, root, elements, {}, set(), parent_id=None, depth=0, clip=clip)
    return [element.name for element in elements]


def box(x, y, w, h):
    return {"AXPosition": (x, y), "AXSize": (w, h)}


def test_walk_skips_elements_outside_the_window_but_not_zero_size_wrappers(monkeypatch):
    inside = Ref("inside", AXRole="AXButton", **box(10, 10, 20, 20))
    outside = Ref("outside", AXRole="AXGroup", AXChildren=[Ref("under-outside", **box(500, 500, 5, 5))],
                  **box(400, 400, 50, 50))
    overflowing = Ref("overflowing", AXRole="AXButton", **box(40, 40, 20, 20))
    wrapper = Ref("wrapper", AXRole="AXGroup", AXChildren=[overflowing], **box(0, 0, 0, 0))
    root = Ref("window", AXRole="AXWindow", AXChildren=[inside, outside, wrapper], **box(0, 0, 100, 100))
    assert walk_tree(monkeypatch, root, clip=Bounds(0, 0, 100, 100)) == ["window", "inside", "wrapper", "overflowing"]


def test_scroll_areas_clip_their_content(monkeypatch):
    shown = Ref("shown", AXRole="AXButton", **box(10, 10, 20, 20))
    scrolled_away = Ref("scrolled-away", AXRole="AXButton", **box(10, 60, 20, 20))
    scroll = Ref("scroll", AXRole="AXScrollArea", AXChildren=[shown, scrolled_away], **box(0, 0, 100, 50))
    root = Ref("window", AXRole="AXWindow", AXChildren=[scroll], **box(0, 0, 100, 100))
    assert walk_tree(monkeypatch, root, clip=Bounds(0, 0, 100, 100)) == ["window", "scroll", "shown"]


def test_lists_walk_their_header_and_visible_rows_only(monkeypatch):
    rows = [Ref(f"row-{i}", AXRole="AXRow") for i in range(5)]
    header = Ref("header", AXRole="AXGroup")
    column = Ref("column", AXRole="AXColumn", AXChildren=rows)
    table = Ref("table", AXRole="AXOutline", AXChildren=[*rows, column, header],
                AXVisibleRows=rows[1:3], AXHeader=header)
    # Web areas answer AXVisibleRows with an empty list; their children are used.
    web = Ref("web", AXRole="AXWebArea", AXChildren=[Ref("link", AXRole="AXLink")], AXVisibleRows=[])
    root = Ref("window", AXRole="AXWindow", AXChildren=[table, web])
    assert walk_tree(monkeypatch, root, clip=None) == ["window", "table", "header", "row-1", "row-2", "web", "link"]


@pytest.mark.parametrize(("value", "queried"), [(None, False), ("Draft", True)])
def test_settable_is_queried_only_for_elements_with_a_value(monkeypatch, value, queried):
    calls = []

    def settable(ref, name, _):
        calls.append(name)
        return 0, False

    api = SimpleNamespace(AXUIElementIsAttributeSettable=settable, AXUIElementCopyActionNames=lambda ref, _: (0, []))
    monkeypatch.setattr(macos_ax, "_frameworks", lambda: (api, None))
    monkeypatch.setattr(macos_ax, "_bounds_from_values", lambda *args: None)
    backend = object.__new__(macos_ax.MacOSAXBackend)
    backend._cache = None
    attributes = {name: None for name in macos_ax._ELEMENT_ATTRIBUTES} | {"AXRole": "AXStaticText", "AXValue": value}
    backend._element_from_ref(Ref("text"), "ax_1", parent_id=None, attributes=attributes)
    assert (calls == ["AXValue"]) is queried


@pytest.mark.parametrize(("row", "clickable", "selected"), [
    (macos_ax.RowState(selected=True), True, True),
    (macos_ax.RowState(selected=None), True, None),
    (None, False, None),
])
def test_text_in_a_row_is_clickable_and_shows_the_row_selection(monkeypatch, row, clickable, selected):
    api = SimpleNamespace(AXUIElementIsAttributeSettable=lambda ref, name, _: (0, False),
                          AXUIElementCopyActionNames=lambda ref, _: (0, ["AXShowMenu"]))
    monkeypatch.setattr(macos_ax, "_frameworks", lambda: (api, None))
    monkeypatch.setattr(macos_ax, "_bounds_from_values", lambda *args: Bounds(0, 0, 80, 20))
    backend = object.__new__(macos_ax.MacOSAXBackend)
    backend._cache = None
    attributes = {name: None for name in macos_ax._ELEMENT_ATTRIBUTES}
    attributes |= {"AXRole": "AXStaticText", "AXValue": "Appearance"}
    element = backend._element_from_ref(Ref("text"), "ax_1", parent_id=None, attributes=attributes, row=row)
    assert (ActionKind.CLICK in element.actions) is clickable
    assert ActionKind.RIGHT_CLICK in element.actions
    assert element.selected is selected


def test_an_element_inside_a_selected_row_stays_fresh(monkeypatch):
    api = SimpleNamespace(AXUIElementIsAttributeSettable=lambda ref, name, _: (0, False),
                          AXUIElementCopyActionNames=lambda ref, _: (0, ["AXShowMenu"]))
    monkeypatch.setattr(macos_ax, "_frameworks", lambda: (api, None))
    monkeypatch.setattr(macos_ax, "_bounds_from_values", lambda *args: Bounds(0, 0, 80, 20))
    attributes = {name: None for name in macos_ax._ELEMENT_ATTRIBUTES}
    attributes |= {"AXRole": "AXStaticText", "AXValue": "General"}
    monkeypatch.setattr(macos_ax, "copy_attributes", lambda api, ref, names: dict(attributes))
    backend = object.__new__(macos_ax.MacOSAXBackend)
    backend._cache = None
    backend.app = fake_app()
    row = macos_ax.RowState(selected=True)
    walked = backend._element_from_ref(Ref("text"), "ax_1", parent_id=None, attributes=attributes, row=row)
    backend._refs, backend._row_states = {"ax_1": Ref("text")}, {"ax_1": row}
    snapshot = DesktopSnapshot(application="Settings", window="General", revision="1", elements=(walked,),
                               context={"pid": 123})
    action = ExecutableAction(kind=ActionKind.CLICK, target_id="ax_1", target_guard=walked.semantic_guard())
    assert walked.selected is True
    assert backend.is_fresh(snapshot, action)


def test_full_tree_request_stops_for_apps_that_do_not_support_it():
    calls = []

    def set_attribute(ref, name, value):
        calls.append(name)
        return macos_ax._AX_ATTRIBUTE_UNSUPPORTED

    backend = object.__new__(macos_ax.MacOSAXBackend)
    backend._cache = None
    backend._full_tree_supported = True
    api = SimpleNamespace(AXUIElementSetAttributeValue=set_attribute)
    backend._request_full_tree(api, Ref("app"))
    backend._request_full_tree(api, Ref("app"))
    assert calls == ["AXManualAccessibility"]


class FakeFeed:
    available = True

    def __init__(self):
        self.changes = []

    def drain(self):
        changes, self.changes = self.changes, []
        return changes

    def close(self):
        pass


def cached_backend(monkeypatch):
    """A backend with the cache on, over fake refs; returns (backend, feed, reads)."""
    from arc_cua.backends.macos_ax_cache import AXNodeCache

    install_cf(monkeypatch)
    reads = []

    def copy(api, ref, names):
        if "AXRole" in names:
            reads.append(ref.identity)
        return {name: ref.attributes.get(name) for name in names}

    monkeypatch.setattr(macos_ax, "_attr", lambda api, ref, name: ref.attributes.get(name))
    monkeypatch.setattr(macos_ax, "copy_attributes", copy)
    monkeypatch.setattr(macos_ax, "_bounds_from_values",
                        lambda api, pos, size: Bounds(*pos, *size) if pos and size else None)
    backend = object.__new__(macos_ax.MacOSAXBackend)
    backend.max_depth, backend.max_elements = 10, 100
    backend._identities = macos_ax._AXIdentityRegistry()
    backend._element_from_ref = lambda ref, identity, **kw: DesktopElement(
        id=identity, role="Group", name=ref.identity, value=ref.attributes.get("AXValue"),
    )
    feed = FakeFeed()
    backend._cache = AXNodeCache(feed)
    return backend, feed, reads


def cached_observe(backend, root, window=Bounds(0, 0, 100, 100)):
    backend._identities.begin_observation()
    backend._cache.begin(None, root, window, lambda ref: ref.attributes.get("AXRole", ""))
    elements = []
    backend._walk(None, root, elements, {}, set(), parent_id=None, depth=0, clip=window)
    return {element.name: element.value for element in elements}


def test_cache_reads_nothing_again_when_nothing_changed(monkeypatch):
    backend, feed, reads = cached_backend(monkeypatch)
    field = Ref("field", AXRole="AXTextField", AXValue="a", **box(10, 10, 20, 20))
    root = Ref("window", AXRole="AXWindow", AXChildren=[field], **box(0, 0, 100, 100))
    cached_observe(backend, root)
    reads.clear()
    assert cached_observe(backend, root) == {"window": None, "field": "a"}
    assert reads == ["window"]  # the window is always re-read, for its title


def test_cache_rereads_an_element_the_app_reports_changed(monkeypatch):
    backend, feed, reads = cached_backend(monkeypatch)
    field = Ref("field", AXRole="AXTextField", AXValue="a", **box(10, 10, 20, 20))
    other = Ref("other", AXRole="AXButton", **box(40, 10, 20, 20))
    root = Ref("window", AXRole="AXWindow", AXChildren=[field, other], **box(0, 0, 100, 100))
    cached_observe(backend, root)
    field.attributes["AXValue"] = "b"
    feed.changes.append((field, "AXValueChanged"))
    reads.clear()
    assert cached_observe(backend, root)["field"] == "b"
    assert sorted(reads) == ["field", "window"]


def test_cache_finds_children_added_without_a_notification(monkeypatch):
    backend, feed, reads = cached_backend(monkeypatch)
    group = Ref("group", AXRole="AXGroup", AXChildren=[], **box(10, 10, 50, 50))
    root = Ref("window", AXRole="AXWindow", AXChildren=[group], **box(0, 0, 100, 100))
    cached_observe(backend, root)
    group.attributes["AXChildren"] = [Ref("spinner", AXRole="AXImage", **box(20, 20, 10, 10))]
    assert "spinner" in cached_observe(backend, root)


def test_cache_rereads_scrolled_content_when_the_scroll_bar_moves(monkeypatch):
    backend, feed, reads = cached_backend(monkeypatch)
    bar = Ref("bar", AXRole="AXScrollBar", AXValue=0.0, **box(90, 0, 10, 50))
    row = Ref("row", AXRole="AXStaticText", AXValue="first", **box(10, 10, 50, 10))
    scroll = Ref("scroll", AXRole="AXScrollArea", AXChildren=[row, bar], **box(0, 0, 100, 50))
    root = Ref("window", AXRole="AXWindow", AXChildren=[scroll], **box(0, 0, 100, 100))
    cached_observe(backend, root)
    row.attributes["AXValue"] = "second"  # content scrolled; only the bar announces it
    feed.changes.append((bar, "AXValueChanged"))
    assert cached_observe(backend, root)["row"] == "second"


def test_cache_starts_over_when_the_window_moves(monkeypatch):
    backend, feed, reads = cached_backend(monkeypatch)
    root = Ref("window", AXRole="AXWindow", AXChildren=[Ref("button", AXRole="AXButton", **box(10, 10, 5, 5))],
               **box(0, 0, 100, 100))
    cached_observe(backend, root)
    reads.clear()
    cached_observe(backend, root, window=Bounds(500, 0, 100, 100))
    assert sorted(reads) == ["button", "window"]


@pytest.mark.parametrize(("available", "expected"), [(True, 5), (False, None)])
def test_settle_probe_is_the_apps_notification_count(monkeypatch, available, expected):
    class Monitor:
        count = 5

        def watch(self, pid):
            assert pid == 123
            return available

    monkeypatch.setattr(macos_ax, "AXEventMonitor", Monitor)
    backend = object.__new__(macos_ax.MacOSAXBackend)
    backend.app, backend._events = fake_app(), None
    assert backend.settle_probe() == expected
