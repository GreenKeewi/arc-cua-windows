# arc-cua

**Superfast action layer for computer-use agents, powered by decision models.**

> Built by [Isle](https://tryisle.com) — managed desktop environments for computer-use agents.

---

`arc-cua` lets a planner or CUA agent hand off bounded desktop subtasks to a fast decision model that executes the UI loop — no frontier model needed for every click.

```python
from arc_cua import execute_payload

result = execute_payload(executor, {
    "goal": "Play Get Lucky by Daft Punk in Spotify",
    "inputs": {"search_query": "Get Lucky Daft Punk"},
    "verification": ["Spotify shows Get Lucky as the current track"],
    "constraints": ["Do not modify the user's library"],
    "max_actions": 15,
})

# result: {"status": "SUBTASK_COMPLETE", "actions_taken": 4}
```

Any GPT, Claude, Gemini, local model, or deterministic planner can generate that payload. The planner deliberately lives outside the package.

---

## Why

Computer-use agents should not need a frontier model to reason about every individual click.

A typical CUA loop:

```text
observe → large model → click → observe → large model → type → observe → large model → click
```

`arc-cua` separates high-level reasoning from low-level execution:

```text
planner / LLM
     ↓
bounded subtask
     ↓
arc-cua
     ↓
JEV → action → action → action → action
     ↓
return to planner
```

The optimization target is **fewer expensive reasoning calls per completed task**, not fewer UI actions.

---

## How it works

```text
any planner / CUA
        |
        | Subtask(goal, inputs, verification, constraints)
        v
+-----------------------+
|       arc-cua         |
|                       |
| observe desktop       |
| AX + local OCR        |
|         v             |
| build legal           |
| action space          |
|         v             |
| JEV decision          |<------+
|         v             |       |
| freshness guard       |       |
|         v             |       |
| execute UI            |       |
|         v             |       |
| wait for UI settle    |-------+
+-----------+-----------+
            |
            v
SUBTASK_COMPLETE / BLOCKED / NEEDS_AGENT
            |
            v
         planner
```

### JEV

JEV is the decision backend that powers the action loop. Given structured desktop state (elements, roles, values), it selects the next UI operation from a dynamically built action space — it can only pick targets and operations the current desktop actually exposes.

JEV is accessed through [TypeSafe](https://typesafe.com). One JEV call can resolve the operation and its parameters in parallel.

### Other decision models

The loop is not tied to JEV. `ChoicePolicy` builds the finite-choice questions and validates the answers; a `ChoiceTransport` sends them to a decision model. `TypeSafeJevPolicy` is `ChoicePolicy` with the TypeSafe transport. Any provider that answers typed choice questions with a choice, confidence and probabilities can be plugged in:

```python
from arc_cua.policies import ChoicePolicy

class MyTransport:
    name = "MyProvider"

    def ask(self, state, questions, *, images=()):
        ...  # return {"answers": {name: {"choice", "confidence", "probabilities"}}}

policy = ChoicePolicy(MyTransport())
```

See [the extension guide](site/llms-full.txt) for the request and answer shapes.

### The agent owns intent

The upstream agent decides what needs to happen, what literal text may be used, what must not happen, and what counts as success. JEV chooses which element to target and which operation to perform — but never invents arbitrary text. Literal values always originate from the agent via `inputs`.

### Caller-supplied shortcuts

Supply extra keyboard shortcuts for an individual subtask, with descriptions that tell JEV what they do:

```python
from arc_cua import Subtask

task = Subtask(
    goal="Save the current document",
    verification=("The document has no unsaved changes",),
    shortcuts={"MOD+S": "Save the current document in this editor"},
)
```

The same `shortcuts` map is accepted by `execute_payload`. JEV receives these choices alongside the existing default hotkeys and chooses a chord when it selects `HOTKEY`. A supplied description can also clarify a default shortcut's meaning in the current app. The defaults are unchanged, and supplied shortcuts apply only to that subtask.

```python
result = execute_payload(executor, {
    "goal": "Save the current document",
    "verification": ["The document has no unsaved changes"],
    "shortcuts": {"MOD+S": "Save the current document in this editor"},
})
```

Chords use uppercase key names and one or more `MOD`, `CTRL`, `ALT`, or `SHIFT` modifiers, for example `MOD+S`, `CTRL+ALT+7`, or `SHIFT+F12`. `MOD` means Command on macOS. Supported keys include A-Z, 0-9, F1-F20, navigation keys, and named punctuation keys; see [the keyboard vocabulary](src/arc_cua/keyboard.py). The macOS backend uses US/ANSI physical key positions. Each shortcut is one chord, not a sequence of actions.

Malformed declarations fail when the subtask is created. JEV can choose only offered chords; runtime validation also rejects hotkeys outside the defaults and the current subtask's declarations, including decisions from custom policies.

### JSON field types

The JSON API validates the same contract as `Subtask` before calling the policy:

| Field | JSON type |
|---|---|
| `goal` | Non-empty string (required) |
| `verification` | Non-empty array of non-empty strings (required) |
| `constraints` | Array of non-empty strings; defaults to `[]` |
| `inputs` | Object mapping non-empty names to literal strings, finite numbers, or booleans |
| `max_actions` | Integer at least 1; defaults to 30 |
| `shortcuts` | Object mapping uppercase chords to non-empty descriptions |
| `metadata` | Object; defaults to `{}` |

For example, use `"verification": ["The folder exists"]`, even for one criterion.
A bare string is rejected rather than split into characters. The Python API accepts
lists or tuples for criteria and constraints, and copies them into tuples. Input
literals are copied into an immutable mapping. Invalid values raise a field-specific
`ValueError`; values are not silently converted from strings to numbers or arrays.

Supply each folder name, filename, or path to be typed as a literal in `inputs`.
Text mentioned only in `goal` cannot be invented as an input by JEV.
Use `MOD+SHIFT+N`, not `Command+Shift+N`; an unmodified Return is the built-in
`PRESS_KEY` value `ENTER`, not an extra hotkey.

### Hybrid macOS perception

`arc-cua` combines two local perception sources:

- **Accessibility (AX)** — semantic controls: buttons, fields, menus, roles, values, native actions
- **Apple Vision OCR** — visible screen text with bounding boxes, for apps with incomplete accessibility

Both normalize into `DesktopElement`s that JEV reasons over. JEV receives structured elements and IDs, not screenshots. Providers that accept images can also receive a window screenshot for completion checks; see [Terminal states](#terminal-states).

The provider request stores element facts once in a shared table. Target choices
refer to those observed IDs, and all questions share the same subtask. This reduces
repeated request data without discarding element facts or changing the offered choices.
Provider token-limit failures include `max_tokens_exceeded` in the returned error;
no UI action is executed for a failed decision request.

### Runtime-owned settling

After a mutating action, `arc-cua` waits until the desktop has reacted and gone quiet, then observes it once. The decision model decides **what to do**; the runtime decides **when the UI is ready to reason over again**.

The macOS backend settles on a cheap visual probe: the frontmost window plus a small grayscale thumbnail of its on-screen area (sheets and panels included). The runtime waits up to `settle_reaction_s` (0.6 s; covers an app still busy with the previous action) for a visible reaction, then until the probe has been unchanged for `settle_quiet_s` (0.15 s), capped at `settle_timeout_s` (2 s). A caret-sized change does not count as activity. Full AX + OCR observations are not used for settling because OCR output varies slightly between passes even when the UI is identical. Backends without a `settle_probe()` method keep snapshot-based settling.

`TYPE_TEXT` can press `ENTER` or `TAB` right after entering its value (`Decision.key`), so a path, search or name field can be filled and submitted in one decision. The runtime waits for the typed value to settle before pressing the key, and the history records the key with the `TYPE_TEXT` action.

`CLICK` can hold a selection modifier (`Decision.click_modifier`): `MOD` (Cmd on macOS) adds the target to or removes it from the current selection, and `SHIFT` extends a range to it. This lets one subtask select several specific items, for example files to copy or move together. On macOS a modified click is always a real mouse event at the element's center, because `AXPress` ignores modifiers.

### Terminal states

| Status | Meaning |
|---|---|
| `SUBTASK_COMPLETE` | Verification criteria appear satisfied |
| `BLOCKED` | Cannot make progress with available operations |
| `NEEDS_AGENT` | Higher-level reasoning required or action budget reached |

The caller owns overall task completion.

The JEV policy checks each supplied verification criterion in a separate choice
head. A proposed completion becomes `NEEDS_AGENT` with a reason if any criterion
is contradicted or cannot be established. These checks are model judgements;
use `RuntimeConfig.verify` or caller-side validation when completion needs an
independent check.

**Screenshot completion checks.** With a provider that accepts images, pass a
screenshot source, such as `ChoicePolicy(transport, screenshot=backend.capture_image)`.
When the model proposes `SUBTASK_COMPLETE`, the policy asks the verification
questions again with a PNG of the current window attached; those answers decide
completion. Ordinary steps send no image, so only a completion costs an extra
request. On macOS the image is the frontmost window's on-screen area, sheets
included, scaled to at most 1280 px on the longest side. JEV does not accept
images, so `TypeSafeJevPolicy` does not offer this.

**Confidence threshold.** `RuntimeConfig(min_confidence=0.6)` returns
`NEEDS_AGENT` instead of acting, or completing, when a decision's confidence is
below the threshold. A `ChoicePolicy` decision's confidence is that of the weakest
answer it uses (operation, target, input, key, completion checks). `BLOCKED` and
`NEEDS_AGENT` are never gated, and decisions without a confidence are not gated.

---

## Install

Currently macOS-first.

```bash
python3.12 -m venv .venv
source .venv/bin/activate

pip install -e '.[macos]'
```

Set your TypeSafe key:

```bash
export TYPESAFE_API_KEY=...
```

### macOS permissions

The terminal/editor running Python needs both:

- **Accessibility** — System Settings → Privacy & Security → Accessibility
- **Screen Recording** — System Settings → Privacy & Security → Screen Recording (required for OCR and screenshot completion checks)

Restart the terminal after granting permissions if necessary.

---

## Examples

### Deterministic architecture demo

No API key required:

```bash
python examples/effects_demo.py
```

### macOS probes

```bash
python examples/macos_ax_probe.py   # Inspect frontmost app's AX tree
python examples/ocr_probe.py        # Inspect visible text via Apple Vision
```

### Spotify

Play a track using OCR-heavy workflow:

```bash
python examples/test_spotify.py
```

### System Settings

Change macOS appearance using Accessibility-heavy workflow:

```bash
python examples/test_settings.py
```

---

## Roadmap

Decision providers: an adapter for OpenAI's Decisions API (announced at DevDay 2026, in limited preview) will be added as a `ChoiceTransport` once its API is published.

AX + OCR covers native and Electron desktop workflows. The next perception frontier is custom graphical interfaces — video timelines, CAD canvases, node graphs, spatial drag targets — which can be added as perception providers while keeping the same `DesktopElement` and execution interfaces.
