# arc-cua internals

Detailed notes on perception, execution, and safety mechanisms.

---

## Desktop snapshot

Every backend normalizes UI state into `DesktopElement`s.

```python
DesktopElement(
    id="ax_91da...",
    role="TextField",
    name="Search Effects",
    value="",
    actions=(
        ActionKind.CLICK,
        ActionKind.TYPE_TEXT,
    ),
    source="macos_ax",
)
```

A `DesktopSnapshot` contains:

- application
- active window
- semantic / visual elements
- context
- revision fingerprint

The decision policy can only select operations and IDs exposed by the current snapshot. It cannot invent arbitrary selectors or coordinates. Coordinates remain a backend implementation detail.

---

## Accessibility

macOS Accessibility provides semantic controls:

```python
DesktopElement(
    id="ax_91da...",
    role="TextField",
    name="Search",
    value="",
    actions=(
        ActionKind.CLICK,
        ActionKind.TYPE_TEXT,
    ),
    source="macos_ax",
)
```

The AX backend can currently:

- inspect the frontmost application and window
- traverse the accessibility tree
- read names, roles, values and state
- invoke native accessibility actions
- focus and edit text controls
- operate buttons and menus
- set supported values
- issue keyboard shortcuts and scrolling
- validate a target immediately before mutation

AX identity uses Core Foundation equality and hashing, with collision checks, rather
than wrapper memory addresses. The normal and modal traversals share those IDs.
References are retained across observations and retired when no longer observed.
Opaque AX reference values are omitted from model-visible text.
Accessible item URLs are included as metadata and in freshness guards, so identical
labels at different destinations are distinguishable.

The observer uses the focused window, falling back to the main window, and includes
the focused element separately when an inline editor lives outside that window's
tree. This avoids traversing inactive application menus during inline editing.

Control bounds are decoded from AXValue geometry. `CLICK` invokes `AXPress` when
available or uses the control's current bounds; `AXShowMenu` is not treated as a
left click. Exposed double-click and right-click operations use current geometry.

---

## Local Apple Vision OCR

Some desktop applications expose little useful accessibility information.

For those interfaces, `arc-cua` captures the target window locally and uses Apple Vision OCR to turn visible screen text into indexed elements.

```python
DesktopElement(
    id="ocr_91ab...",
    role="visible_text",
    name="Get Lucky",
    bounds=Bounds(...),
    actions=(
        ActionKind.CLICK,
        ActionKind.DOUBLE_CLICK,
    ),
    source="macos_ocr",
)
```

The screenshot is processed locally. JEV receives structured text elements and IDs, not the screenshot itself.
Only screenshot completion checks (below) send an image, and only to a provider that accepts images.

### Visual text-entry targets

OCR does not automatically mean a region is editable.

A region such as `What do you want to play` may be classified as a plausible visual input and expose `CLICK`, `DOUBLE_CLICK`, `RIGHT_CLICK`, `TYPE_TEXT` — while ordinary visible labels remain click-only.

This prevents every OCR string on the screen from becoming an arbitrary typing target.

---

## OCR stability

OCR output is inherently noisy. The same Spotify search field may be recognized across frames as:

```text
What do you want to play
What doyou want to plafP
Q Whatdoyouwantto play
```

`arc-cua` avoids using exact OCR text as visual identity. OCR regions use coarse spatial identity, and overlapping detections are deduplicated before they are exposed to JEV.

OCR text that an actionable accessibility element already represents is also dropped,
so one control is offered under one id, with the stronger AX semantics. The OCR
region's center must lie inside the element's bounds, and its normalized text
(3+ characters) must either appear in the element's name, or cover at least half
of its value. Names match by containment because fast OCR often reads a truncated
label, such as `Norm` for a tab titled `Normal | Applied research`. Values need
the coverage rule because a terminal or document exposes all of its text as one
value; its individual lines stay targetable through OCR. In a Chrome window this
removed 3 of 82 OCR regions, since Chrome exposes most page content as unnamed
groups, and nothing in a terminal.

This keeps small OCR fluctuations from looking like entirely new UI state.

---

## Dynamic JEV action space

The JEV policy builds its choices dynamically from the current desktop state.

A request may contain questions like:

```text
operation:
  CLICK / DOUBLE_CLICK / RIGHT_CLICK / TYPE_TEXT / SET_VALUE / PRESS_KEY / HOTKEY / SCROLL
  SUBTASK_COMPLETE / BLOCKED / NEEDS_AGENT

click_target:
  element_4 / element_7 / element_12

type_text_target:
  element_7

type_text_input:
  effect_name / filename

type_text_then_key:
  NONE / ENTER / TAB

click_modifier:
  NONE / MOD / SHIFT
```

One JEV request can ask for the operation and speculative operation-specific choices in parallel. Only the head corresponding to the selected operation is consumed.

These questions are provider-neutral. `ChoicePolicy` (`policies/choice.py`) builds
them and validates the answers; a `ChoiceTransport` sends them. `TypeSafeTransport`
(`policies/typesafe.py`) adds the model name, posts to TypeSafe, and retries rate
limits. An invalid answer raises `Invalid <provider> choice response` and no action
is executed.

The decision's `confidence` is the lowest confidence among the answers it uses,
such as operation, target and input for `TYPE_TEXT`, or operation plus every
verification answer for `SUBTASK_COMPLETE`. Speculative heads that were not
consumed do not affect it. `RuntimeConfig.min_confidence` compares against this
value before any action or completion is accepted.

`Decision.margin` is the smallest lead of the chosen option's probability over the
runner-up's among the same answers. `RuntimeConfig.min_margin` refuses near-ties,
including exact ties, which answer validation otherwise accepts.

### Subtask shortcuts

`subtask_from_dict` and `Subtask` validate the same field types. Verification and
constraints must be lists/tuples of non-empty strings (verification cannot be empty).
They are copied to tuples; strings are never iterated into individual characters.
Input values must be literal strings, finite numbers or booleans, with non-empty
string keys; the input mapping is copied and frozen. The JSON boundary rejects
missing/unknown fields and does not coerce invalid goals or action budgets.

The JEV wire representation puts compact element fields into a shared table:
`state.desktop.element_columns` names the columns and `elements` holds the rows.
Null and omitted trailing columns mean absent fields. Target question criteria
retain the observed IDs and refer to those rows. The subtask is stored once in
`state.subtask`. This preserves compact element facts and the action candidate sets;
it does not prune the desktop to save tokens. Public snapshots and traces still use
their ordinary object representation. A provider token-limit rejection surfaces
as an actionable `max_tokens_exceeded` error without executing an action.

`Subtask.shortcuts` is a mapping from a keyboard chord to its description, such as `{"MOD+S": "Save the current document"}`. The Python and JSON APIs accept the same mapping. It is validated and copied into an immutable mapping when the subtask is created; `compact()` returns a JSON-compatible copy.

For each decision, the policy builds `hotkey_value` from `DEFAULT_HOTKEYS` plus the current subtask's shortcuts. The chord is the choice ID and the description is its criterion. Caller descriptions override descriptions for matching defaults. No choices are stored on the policy or inherited by another subtask.

JEV's selected chord is validated against that request's choices, and `materialize_action` independently checks it against the defaults plus the subtask's declarations. This also applies to custom decision policies: declare any non-default hotkey in the subtask before emitting it. The macOS backend encodes the selected chord using its general key map and modifier flags; it does not implement app-specific task sequences.

---

## Text entry

Literal text always originates from the upstream agent.

For AX text controls, a writable `AXValue` alone is insufficient: the control must
also support focus or editable text selection, or already hold focus. Some item
labels advertise writable values that change only the display, not the underlying
item. Such labels expose navigation/selection actions, not text-entry actions;
the policy must activate a real editor before entering the supplied value.
Committing an edit remains a separate UI action selected by the policy.
Recent action history includes the actual keys, hotkeys, and scroll directions so
the policy can distinguish attempted operations and avoid repeating them.

For OCR-backed inputs, `arc-cua` uses the same macOS text-delivery strategy from Third Hand:

```text
focus visual input → Cmd+A → brief settle → emit Unicode CGEvent key-down/up one character at a time
```

Modifier flags are explicitly cleared for each Unicode event so the preceding `Cmd+A` cannot leak into the typed text.

The decision model chooses `input_key = search_query`. The runtime supplies `Subtask.inputs["search_query"]`. The decision model never invents arbitrary text.

---

## Freshness protection

Every actionable target has a semantic or visual guard.

Before executing a chosen mutation, the backend checks that the target still corresponds to the UI state JEV observed.

```text
observe → JEV decides → target changes before execution → discard decision → observe again
```

A stale action is never blindly replayed. The runtime also consumes each decision before mutation so a successful action cannot accidentally execute twice during a retry.

## Completion checks

The JEV request includes one verification head per caller-supplied criterion,
alongside the operation and target heads. Each classifies the current evidence as
`SATISFIED`, `NOT_SATISFIED`, or `UNKNOWN`. The policy accepts a proposed
`SUBTASK_COMPLETE` only when every head says `SATISFIED`; otherwise it returns
`NEEDS_AGENT` and a reason listing the unverified criteria. Missing or malformed
verification answers fail validation rather than authorizing completion.

This is a consistency check between model decisions, not proof of application
state. Callers should inspect results and can supply `RuntimeConfig.verify` for an
independent domain-specific check. A policy's optional `Decision.reason` is
preserved in the terminal execution result.

### Screenshots on every decision

`ChoicePolicy(transport, screenshot_steps=True)` sends `snapshot.screenshot()` with
every request and adds `state.image`, a note that the image was captured with the
element table and that its text is untrusted. Targets are still offered ids only.
A snapshot without a screenshot raises before any request. When both options are
set, the completion checks in the same request have already seen the image, so
`screenshot_checks` makes no second request.

### Screenshot completion checks

With `ChoicePolicy(transport, screenshot_checks=True)`, the transport must set
`supports_images = True`. After the first request proposes `SUBTASK_COMPLETE`,
the policy calls `snapshot.screenshot()` and sends a second request with the same
state, only the verification questions, and the PNG in `images`. Their instructions
add that the image was captured with the element table and takes precedence when
they disagree. The second request's answers replace the first request's
verification answers. Its response is kept in `Decision.raw["image_verification"]`,
and its latency is added to the decision's. A snapshot without a screenshot raises,
so completion is never accepted unchecked.

The image is bound to the snapshot: `MacOSHybridBackend.observe()` keeps the window
image that OCR read and sets `DesktopSnapshot.screenshot` to encode it on demand
(scaled to at most 1280 px on the longest side; about 25 ms on Apple Silicon). A
later capture could show a state the model never saw.

## Chrome backend

`ChromeBackend` (`backends/chrome.py`) implements the same `DesktopBackend`
protocol for one Chrome tab. It talks to Chrome over one browser-level DevTools
websocket (`backends/cdp.py`) in flattened session mode. All page-side work is one
script, `backends/chrome_page.js`, evaluated with a method name; it installs its
state on the page's window on first use.

### Observation

One script call walks the document in order, including open shadow roots and
same-origin iframes (with their offsets added to element bounds), and returns:

- **Interactive elements**: native controls, links, elements with an interactive
  ARIA role, focusable non-container elements, `onclick` elements, and the outermost
  element of a custom widget that shows a pointer cursor. Container roles such as
  `listbox` or `menu` are not targets; their items are. Descendants of an interactive
  element are not listed separately.
- **Dialogs**: `<dialog open>`, `role=dialog/alertdialog` and `aria-modal` elements,
  listed first and used as `parent_id` for elements inside them.
- **Text**: headings, live regions, and text blocks whose children are all
  inline, excluding text inside interactive elements and labels of controls.
  Live regions (`role` `status`, `alert` or `log`, or `aria-live` polite or
  assertive) are included even outside the viewport, marked
  `metadata["offscreen"] = true`, because they carry the confirmations and errors
  that completion checks depend on; a screen reader announces them wherever they are.

Only elements that intersect the viewport and pass `checkVisibility` (opacity and
visibility included), outside `aria-hidden` and `inert` subtrees, are listed.
Interactive elements are capped at 250 and text at 120; omitted counts are in
`context["omitted_elements"]`.

Names follow a practical subset of the accessible-name algorithm:
`aria-labelledby`, `aria-label`, associated `<label>`s, `placeholder`/`title`, `alt`,
then visible text or a descendant image's alt text. Checkbox and radio values are
booleans; field values are their text (passwords are masked). A visually hidden
checkbox or radio is located and clicked through its label.

Each DOM node keeps one id (`w<n>`) for as long as it stays in the document, held
in a page-side `WeakMap`. A navigation starts a new page script, so earlier ids no
longer resolve.

### Covered targets

For each interactive element, the script hit-tests its center and four inner
points with `elementFromPoint`, descending through shadow roots and same-origin
frames. If none reaches the element (or its label), it is listed with no actions
and `metadata["covered"] = true`. Before a pointer action, the element is scrolled
into view if needed and hit-tested again; a covered or missing target raises
`StaleDesktopState` and the runtime re-observes.

### Actions

| Action | How it is performed |
|---|---|
| `CLICK`, `DOUBLE_CLICK`, `RIGHT_CLICK` | DevTools mouse events at the reachable point, with `MOD`/`SHIFT` modifier bits |
| `TYPE_TEXT` | Click the field, select its contents, then `Input.insertText` with the literal |
| `SET_VALUE` | For `<select>`, range and date-like inputs: set the value through the native setter and dispatch `input` and `change` |
| `PRESS_KEY`, `HOTKEY` | DevTools key events with DOM key, code and key code; `MOD` is Meta on macOS and Ctrl elsewhere |
| `SCROLL` | A mouse wheel event at the viewport center, 80% of the viewport |

On macOS, Chrome implements editing shortcuts (`MOD+A/C/X/V/Z`, `MOD+SHIFT+Z`) in
the browser process, so those key events carry the editing command explicitly.
A `<select>` value matches an option label (case-insensitive) or value; no match
fails the action.

### Freshness

`is_fresh` re-describes the target with the same script and compares its
`semantic_guard()`, so a changed name, value, state or link URL is stale.
Untargeted actions require the same page URL. `execute` checks the guard again
before acting.

### Settling

`settle_probe()` returns the URL, `document.readyState`, a DOM mutation counter
(inline `style` changes excluded, so JavaScript animations do not look like
activity), the focused element and the scroll position. During a navigation the
probe returns `("unavailable",)`, which the runtime treats as a change and keeps
waiting on.

### Dialogs and new tabs

A JavaScript dialog blocks the page's script, so the backend tracks
`Page.javascriptDialogOpening`. Input commands stop waiting when a dialog opens,
because Chrome withholds their responses until it closes. While a dialog is open,
`observe()` returns it as `dialog_message` with `dialog_accept` and, except for
`alert`, `dialog_dismiss` buttons, answered with `Page.handleJavaScriptDialog`.

When the tab opens another page (for example a `target=_blank` link), the backend
activates and attaches to it at the next observation, and reports
`context["switched_to_new_tab"] = True`.
