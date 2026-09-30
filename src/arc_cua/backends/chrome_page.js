// Page-side half of arc-cua's Chrome backend. Evaluated as
// `(<this function>)(method, args)`; it installs its state on first use.
(method, args) => {
  const TOP = window;

  const state = TOP.__arcCua || (TOP.__arcCua = (() => {
    const s = { ids: new WeakMap(), nodes: new Map(), next: 1, mutations: 0 };
    // Settling signal: DOM changes other than inline style (JS animations).
    new MutationObserver((records) => {
      for (const r of records) {
        if (r.type !== "attributes" || r.attributeName !== "style") { s.mutations += 1; return; }
      }
    }).observe(document, { subtree: true, childList: true, characterData: true, attributes: true });
    return s;
  })());

  const idOf = (el) => {
    let id = state.ids.get(el);
    if (!id) {
      id = "w" + state.next++;
      state.ids.set(el, id);
    }
    state.nodes.set(id, el);
    return id;
  };

  const nodeOf = (id) => {
    const el = state.nodes.get(id);
    if (!el || !el.isConnected) { state.nodes.delete(id); return null; }
    return el;
  };

  const clean = (text, limit = 160) => {
    const s = (text || "").replace(/\s+/g, " ").trim();
    return s.length > limit ? s.slice(0, limit - 1) + "…" : s;
  };

  // ---- roles -------------------------------------------------------------

  const TEXT_INPUT_TYPES = new Set(["", "text", "search", "email", "url", "tel", "password", "number"]);
  const VALUE_INPUT_TYPES = new Set(["range", "date", "time", "datetime-local", "month", "week", "color"]);
  const CLICK_ROLES = new Set([
    "button", "link", "checkbox", "radio", "switch", "tab", "menuitem", "menuitemcheckbox",
    "menuitemradio", "option", "treeitem", "gridcell", "row",
  ]);
  const TYPE_ROLES = new Set(["textbox", "searchbox", "combobox", "spinbutton"]);

  const roleOf = (el) => {
    const explicit = (el.getAttribute("role") || "").trim().split(/\s+/)[0];
    if (explicit && explicit !== "presentation" && explicit !== "none" && explicit !== "generic") return explicit;
    const tag = el.localName;
    if (tag === "a" && el.hasAttribute("href")) return "link";
    if (tag === "button" || tag === "summary") return "button";
    if (tag === "select") return el.multiple || el.size > 1 ? "listbox" : "combobox";
    if (tag === "textarea") return "textbox";
    if (tag === "input") {
      const type = (el.getAttribute("type") || "").toLowerCase();
      if (type === "checkbox" || type === "radio") return type;
      if (["button", "submit", "reset", "image"].includes(type)) return "button";
      if (type === "range") return "slider";
      if (type === "number") return "spinbutton";
      if (type === "search") return "searchbox";
      if (el.hasAttribute("list")) return "combobox";
      return "textbox";
    }
    if (/^h[1-6]$/.test(tag)) return "heading";
    if (tag === "dialog") return "dialog";
    if (tag === "img") return "img";
    if (el.isContentEditable && (!el.parentElement || !el.parentElement.isContentEditable)) return "textbox";
    return "";
  };

  // ---- accessible names (a practical subset of accname) --------------------

  const textFrom = (el) => clean(el.innerText || el.textContent || "");

  const nameOf = (el, role) => {
    const doc = el.ownerDocument;
    const labelledby = el.getAttribute("aria-labelledby");
    if (labelledby) {
      const text = labelledby.split(/\s+/).map((id) => doc.getElementById(id)).filter(Boolean)
        .map(textFrom).join(" ");
      if (clean(text)) return clean(text);
    }
    const aria = clean(el.getAttribute("aria-label"));
    if (aria) return aria;
    const tag = el.localName;
    if (tag === "input" || tag === "textarea" || tag === "select") {
      const labels = el.labels ? [...el.labels].map(textFrom).filter(Boolean) : [];
      if (labels.length) return clean(labels.join(" "));
      const type = (el.getAttribute("type") || "").toLowerCase();
      if (["button", "submit", "reset"].includes(type)) return clean(el.value) || (type === "submit" ? "Submit" : "");
      if (type === "image") return clean(el.alt);
      return clean(el.getAttribute("placeholder") || el.title);
    }
    if (tag === "img") return clean(el.alt || el.title);
    if (role === "textbox" && el.isContentEditable) return clean(el.title || el.getAttribute("placeholder"));
    let text = textFrom(el);
    if (!text) {
      const inner = el.querySelector("img[alt], svg[aria-label], [aria-label]");
      text = inner ? clean(inner.getAttribute("alt") || inner.getAttribute("aria-label")) : "";
      if (!text) {
        const title = el.querySelector("svg title");
        text = title ? clean(title.textContent) : "";
      }
    }
    return text || clean(el.title);
  };

  // ---- geometry, visibility, hit testing ---------------------------------

  // Offset of an element's own viewport within the top-level viewport.
  const frameOffset = (el) => {
    let x = 0, y = 0;
    let win = el.ownerDocument.defaultView;
    while (win && win !== TOP) {
      const frame = win.frameElement;
      if (!frame) break;
      const r = frame.getBoundingClientRect();
      x += r.left + frame.clientLeft;
      y += r.top + frame.clientTop;
      win = frame.ownerDocument.defaultView;
    }
    return { x, y };
  };

  // Visually hidden checkboxes and radios are usually operated through their label.
  const proxyOf = (el) => {
    if (el.localName === "input" && ["checkbox", "radio"].includes((el.type || "").toLowerCase())) {
      const r = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      const hidden = r.width < 4 || r.height < 4 || Number(style.opacity) < 0.05;
      if (hidden && el.labels && el.labels.length) return el.labels[0];
    }
    return el;
  };

  const boxOf = (el) => {
    const r = el.getBoundingClientRect();
    const o = frameOffset(el);
    return { x: r.left + o.x, y: r.top + o.y, w: r.width, h: r.height };
  };

  const inViewport = (b) =>
    b.w >= 1 && b.h >= 1 && b.x < TOP.innerWidth && b.y < TOP.innerHeight && b.x + b.w > 0 && b.y + b.h > 0;

  const shown = (el) => {
    if (el.closest("[aria-hidden='true'], [inert]")) return false;
    if (el.checkVisibility) {
      return el.checkVisibility({ opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true });
    }
    const style = getComputedStyle(el);
    return style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity) > 0.05;
  };

  // The deepest element at a top-level viewport point, through same-origin
  // frames and open shadow roots.
  const deepHit = (x, y) => {
    let doc = TOP.document, ox = 0, oy = 0, hit = null;
    for (;;) {
      hit = doc.elementFromPoint(x - ox, y - oy);
      while (hit && hit.shadowRoot) {
        const inner = hit.shadowRoot.elementFromPoint(x - ox, y - oy);
        if (!inner || inner === hit) break;
        hit = inner;
      }
      if (hit && (hit.localName === "iframe" || hit.localName === "frame")) {
        let inner = null;
        try { inner = hit.contentDocument; } catch (e) { inner = null; }
        if (inner) {
          const r = hit.getBoundingClientRect();
          ox += r.left + hit.clientLeft;
          oy += r.top + hit.clientTop;
          doc = inner;
          continue;
        }
      }
      return hit;
    }
  };

  const composedContains = (ancestor, node) => {
    for (let n = node; n; n = n.parentNode || n.host) if (n === ancestor) return true;
    return false;
  };

  const reaches = (el, hit) => {
    if (!hit) return false;
    if (composedContains(el, hit)) return true;
    // A label activates its control.
    const label = hit.closest && hit.closest("label");
    return Boolean(label && label.control === el);
  };

  // A point on the element that a pointer event would actually reach.
  const clickPoint = (el) => {
    const target = proxyOf(el);
    const b = boxOf(target);
    const left = Math.max(b.x, 0), top = Math.max(b.y, 0);
    const right = Math.min(b.x + b.w, TOP.innerWidth), bottom = Math.min(b.y + b.h, TOP.innerHeight);
    if (right - left < 1 || bottom - top < 1) return null;
    const cx = (left + right) / 2, cy = (top + bottom) / 2;
    const points = [[cx, cy], [left + 2, top + 2], [right - 2, top + 2], [left + 2, bottom - 2], [right - 2, bottom - 2]];
    for (const [x, y] of points) {
      if (reaches(el, deepHit(x, y))) return { x, y };
    }
    return null;
  };

  // ---- element records ------------------------------------------------------

  const stateOf = (el, role) => {
    const aria = (name) => {
      const v = el.getAttribute(name);
      return v === "true" ? true : v === "false" ? false : null;
    };
    let value = null;
    const tag = el.localName;
    if (role === "checkbox" || role === "radio" || role === "switch" || role === "menuitemcheckbox" || role === "menuitemradio") {
      value = tag === "input" ? el.checked : aria("aria-checked");
    } else if (tag === "select") {
      value = [...el.selectedOptions].map((o) => clean(o.label, 80)).join(", ");
    } else if (tag === "input" || tag === "textarea") {
      value = (el.type || "").toLowerCase() === "password" ? (el.value ? "••••" : "") : clean(el.value, 200);
    } else if (role === "textbox" && el.isContentEditable) {
      value = clean(el.innerText, 200);
    } else if (role === "slider" || role === "spinbutton") {
      value = el.getAttribute("aria-valuenow");
    }
    let expanded = aria("aria-expanded");
    if (tag === "details") expanded = el.open;
    if (tag === "summary" && el.parentElement && el.parentElement.localName === "details") expanded = el.parentElement.open;
    let selected = aria("aria-selected");
    if (tag === "option") selected = el.selected;
    const disabled = el.disabled === true || aria("aria-disabled") === true || Boolean(el.closest("fieldset:disabled"));
    return { value, expanded, selected, enabled: !disabled };
  };

  const actionsOf = (el, role) => {
    const tag = el.localName;
    const type = tag === "input" ? (el.getAttribute("type") || "").toLowerCase() : "";
    if (tag === "select") return ["SET_VALUE"];
    if (tag === "input" && VALUE_INPUT_TYPES.has(type)) return ["SET_VALUE"];
    if (tag === "input" && type === "file") return [];
    if ((tag === "input" && TEXT_INPUT_TYPES.has(type)) || tag === "textarea") {
      return el.readOnly ? ["CLICK"] : ["CLICK", "TYPE_TEXT"];
    }
    if (TYPE_ROLES.has(role) && el.isContentEditable) return ["CLICK", "TYPE_TEXT"];
    const actions = ["CLICK"];
    if (el.hasAttribute("ondblclick")) actions.push("DOUBLE_CLICK");
    return actions;
  };

  const isInteractive = (el, role) => {
    // Containers such as listbox, menu and tablist are not targets; their items are.
    if (CLICK_ROLES.has(role) || TYPE_ROLES.has(role) || role === "slider") return true;
    const tag = el.localName;
    if (tag === "input" || tag === "select" || tag === "textarea" || tag === "button" || tag === "summary") return true;
    if (tag === "a" && el.hasAttribute("href")) return true;
    if (el.isContentEditable && role === "textbox") return true;
    if (el.hasAttribute("onclick")) return true;
    // Focusable elements count, except layout containers made focusable for scrolling.
    const tabindex = el.getAttribute("tabindex");
    return tabindex !== null && Number(tabindex) >= 0 && !CONTAINERS.has(tag);
  };

  const CONTAINERS = new Set(["div", "section", "main", "article", "aside", "nav", "ul", "ol", "table", "body", "html"]);

  // Custom widgets without semantics: the outermost element showing a pointer cursor.
  const isPointerWidget = (el) => {
    if (getComputedStyle(el).cursor !== "pointer") return false;
    const parent = el.parentElement;
    return !parent || getComputedStyle(parent).cursor !== "pointer";
  };

  const INLINE = new Set([
    "a", "abbr", "b", "bdi", "bdo", "br", "cite", "code", "data", "del", "dfn", "em", "i", "img", "ins",
    "kbd", "mark", "q", "s", "samp", "small", "span", "strong", "sub", "sup", "svg", "time", "u", "var", "wbr",
  ]);

  // Blocks whose own text is visible and whose children are all inline.
  const isTextBlock = (el) => {
    if (INLINE.has(el.localName) && el.localName !== "span") return false;
    let direct = false;
    for (const child of el.childNodes) {
      if (child.nodeType === 3 && child.textContent.trim()) direct = true;
      else if (child.nodeType === 1 && !INLINE.has(child.localName)) return false;
    }
    return direct;
  };

  const DIALOG_SELECTOR = "dialog[open], [role='dialog'], [role='alertdialog'], [aria-modal='true']";

  const record = (el, role, box, extra) => {
    const st = stateOf(el, role);
    const rec = {
      id: idOf(el),
      role,
      name: nameOf(el, role),
      value: st.value,
      enabled: st.enabled,
      focused: (el.getRootNode().activeElement || el.ownerDocument.activeElement) === el,
      selected: st.selected,
      expanded: st.expanded,
      bounds: box,
      metadata: {},
      ...extra,
    };
    const tag = el.localName;
    rec.metadata.tag = tag;
    if (tag === "input") rec.metadata.type = (el.getAttribute("type") || "text").toLowerCase();
    if (tag === "a" && el.href) rec.metadata.url = el.href;
    if (tag === "select") {
      rec.metadata.options = [...el.options].slice(0, 40).map((o) => clean(o.label, 60));
      rec.metadata.value_type = "text";
    }
    if (tag === "input" && VALUE_INPUT_TYPES.has(rec.metadata.type)) {
      rec.metadata.value_type = rec.metadata.type === "range" ? "number" : "text";
      if (rec.metadata.type === "range") {
        rec.metadata.min = el.min || "0";
        rec.metadata.max = el.max || "100";
      }
    }
    if (role === "heading") rec.metadata.level = Number((el.getAttribute("aria-level") || tag.slice(1)) || 2);
    const dialog = el.parentElement && el.parentElement.closest(DIALOG_SELECTOR);
    if (dialog && state.ids.get(dialog)) rec.parent = state.ids.get(dialog);
    return rec;
  };

  // Every element in document order, through open shadow roots and same-origin frames.
  const allElements = (root, out) => {
    const walker = (root.ownerDocument || root).createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    for (let el = walker.nextNode(); el; el = walker.nextNode()) {
      out.push(el);
      if (el.shadowRoot) allElements(el.shadowRoot, out);
      if (el.localName === "iframe" || el.localName === "frame") {
        let doc = null;
        try { doc = el.contentDocument; } catch (e) { doc = null; }
        if (doc && doc.documentElement) allElements(doc.documentElement, out);
      }
    }
    return out;
  };

  const observe = ({ maxInteractive = 250, maxText = 120, force = false } = {}) => {
    if (document.readyState === "loading" && !force) return { loading: true };
    const interactive = [], texts = [], dialogs = [];
    let skippedInteractive = 0, skippedText = 0;
    const claimed = new WeakSet();   // text already represented by an interactive element

    for (const el of allElements(document.documentElement, [])) {
      if (el.localName === "iframe" || el.localName === "frame") continue;
      const role = roleOf(el);
      const inside = el.parentElement && claimed.has(el.parentElement);
      if (role === "dialog" || role === "alertdialog" || el.matches(DIALOG_SELECTOR)) {
        const box = boxOf(el);
        if (inViewport(box) && shown(el)) dialogs.push(record(el, role || "dialog", box, { actions: [] }));
        continue;
      }
      let interactiveHere = isInteractive(el, role);
      if (!interactiveHere && !inside && !role && inViewport(boxOf(el))) interactiveHere = isPointerWidget(el);
      if (interactiveHere) {
        const target = proxyOf(el);
        const box = boxOf(target);
        if (!inViewport(box) || !shown(target)) continue;
        claimed.add(el);
        if (interactive.length >= maxInteractive) { skippedInteractive++; continue; }
        const covered = !clickPoint(el);
        const rec = record(el, role || "clickable", box, { actions: covered ? [] : actionsOf(el, role) });
        if (covered) rec.metadata.covered = true;
        interactive.push(rec);
        continue;
      }
      if (inside) { claimed.add(el); continue; }
      const live = role === "alert" || role === "status" || role === "log"
        || ["polite", "assertive"].includes(el.getAttribute("aria-live"));
      if (role === "heading" || isTextBlock(el) || live) {
        // A control's label is already its name.
        const label = el.closest("label");
        if (label && label.control) continue;
        const box = boxOf(el);
        // Live regions (status messages, alerts) count wherever they are, as a
        // screen reader would announce them; other text only when in view.
        if (!(live || inViewport(box)) || !shown(el)) continue;
        claimed.add(el);
        const name = role === "heading" || live ? textFrom(el) : clean(el.innerText, 200);
        if (!name) continue;
        if (texts.length >= maxText) { skippedText++; continue; }
        const rec = { ...record(el, role || (live ? "status" : "text"), box, { actions: [] }), name, value: null };
        if (!inViewport(box)) rec.metadata.offscreen = true;
        texts.push(rec);
      }
    }
    const scroller = document.scrollingElement || document.documentElement;
    return {
      url: location.href,
      title: document.title,
      viewport: [TOP.innerWidth, TOP.innerHeight],
      scroll: [Math.round(scroller.scrollLeft), Math.round(scroller.scrollTop)],
      more_below: scroller.scrollTop + TOP.innerHeight < scroller.scrollHeight - 2,
      more_above: scroller.scrollTop > 2,
      elements: [...dialogs, ...interactive, ...texts],
      omitted: { interactive: skippedInteractive, text: skippedText },
    };
  };

  // Re-describe one known element for freshness checks.
  const describe = ({ id }) => {
    const el = nodeOf(id);
    if (!el) return null;
    const role = roleOf(el) || (el.matches(DIALOG_SELECTOR) ? "dialog" : "");
    const interactive = isInteractive(el, role) || isPointerWidget(el);
    const box = boxOf(proxyOf(el));
    if (!interactive) return record(el, role || "text", box, { actions: [] });
    return record(el, role || "clickable", box, { actions: actionsOf(el, role) });
  };

  // Scroll the element into view if needed and return a reachable point.
  const point = ({ id }) => {
    const el = nodeOf(id);
    if (!el) return { error: "missing" };
    let p = clickPoint(el);
    if (!p) {
      proxyOf(el).scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
      p = clickPoint(el);
    }
    return p ? p : { error: "covered" };
  };

  // Select existing content so inserted text replaces it.
  const selectContents = ({ id }) => {
    const el = nodeOf(id);
    if (!el) return { error: "missing" };
    el.focus({ preventScroll: true });
    if (typeof el.select === "function") {
      el.select();
    } else if (el.isContentEditable) {
      const doc = el.ownerDocument;
      const range = doc.createRange();
      range.selectNodeContents(el);
      const selection = doc.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    }
    return { ok: true };
  };

  // Set a native control's value the way user input would, notifying frameworks.
  const setValue = ({ id, value }) => {
    const el = nodeOf(id);
    if (!el) return { error: "missing" };
    const text = String(value);
    let next = text;
    if (el.localName === "select") {
      const wanted = clean(text).toLowerCase();
      const option = [...el.options].find((o) => clean(o.label).toLowerCase() === wanted)
        || [...el.options].find((o) => o.value === text);
      if (!option) return { error: "no_option" };
      next = option.value;
    }
    const proto = Object.getPrototypeOf(el);
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    el.focus({ preventScroll: true });
    if (setter) setter.call(el, next); else el.value = next;
    if (el.localName !== "select" && String(el.value) !== next) return { error: "rejected" };
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { ok: true };
  };

  const probe = () => {
    const active = document.activeElement;
    const scroller = document.scrollingElement || document.documentElement;
    return [
      location.href,
      document.readyState,
      state.mutations,
      active ? state.ids.get(active) || active.localName : null,
      Math.round(scroller.scrollLeft),
      Math.round(scroller.scrollTop),
    ];
  };

  const methods = { observe, describe, point, selectContents, setValue, probe };
  return methods[method](args || {});
}
