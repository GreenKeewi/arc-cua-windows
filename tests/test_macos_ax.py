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


def test_modal_discovery_deduplicates_distinct_wrappers_for_same_sheet(monkeypatch):
    install_cf(monkeypatch)
    ids = macos_ax._AXIdentityRegistry()
    app = Ref("app", AXFocusedWindow=Ref("sheet", AXRole="AXSheet"),
              AXWindows=[Ref("sheet", AXRole="AXSheet")])
    monkeypatch.setattr(macos_hybrid, "_ax_copy_attribute", lambda api, ref, name: ref.attributes.get(name))
    roots = macos_hybrid._find_modal_roots(SimpleNamespace(), app, ids.id_for)
    assert len(roots) == 1


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
    backend.app = fake_app(ax=app)
    backend.max_depth, backend.max_elements = 10, 10
    backend._identities = macos_ax._AXIdentityRegistry()
    backend._element_from_ref = lambda ref, identity, **kw: DesktopElement(
        id=identity, role="Window", name=ref.identity,
    )
    snapshot = backend.observe()
    assert snapshot.window == "This desktop"
    assert snapshot.context["pid"] == 123


def test_observe_fails_clearly_when_the_app_has_no_usable_window(monkeypatch):
    def no_window():
        raise TargetUnavailable("Editor has no open window on this desktop.")

    backend = object.__new__(macos_ax.MacOSAXBackend)
    backend.app = fake_app(windows=lambda: [], require_window=no_window)
    monkeypatch.setattr(macos_ax, "_frameworks", lambda: (SimpleNamespace(), None))
    with pytest.raises(TargetUnavailable, match="no open window"):
        backend.observe()


def test_freshness_fails_clearly_once_the_app_quits():
    def quit():
        raise TargetUnavailable("Editor (process ID 123) quit.")

    backend = object.__new__(macos_ax.MacOSAXBackend)
    backend.app = fake_app(check_running=quit)
    snapshot = DesktopSnapshot(application="Editor", window="Doc", revision="1", elements=(), context={"pid": 123})
    with pytest.raises(TargetUnavailable, match="quit"):
        backend.is_fresh(snapshot, ExecutableAction(kind=ActionKind.PRESS_KEY, key="ENTER"))
