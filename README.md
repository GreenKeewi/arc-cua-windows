# arc-cua Windows MVP preview

Public Windows fork of [shhivv/arc-cua](https://github.com/shhivv/arc-cua), preserving Shiv Shanmugam's MIT license and upstream authorship.

**[Windows setup and model-free demo](docs/windows.md)** · **[Corrected preview download](https://github.com/GreenKeewi/arc-cua-windows/releases/tag/windows-preview-1a72de79831e)** · **[CI evidence](https://github.com/GreenKeewi/arc-cua-windows/actions)**

Adds named UIA controls, foreground clicks/text/scroll/shortcuts, the existing decision loop, CLI and local MCP. Requires a visible, restored Windows desktop. No seamless background/minimized control or Windows OCR. Downloads run locally; this repository does not provide a hosted Windows desktop. The model-free WinForms demo passed on a hosted Windows Server 2025 desktop; Windows 10/11 app validation remains manual. See [test and desktop evidence](docs/windows-validation.md).

The original upstream documentation follows; its macOS background-control claims apply only to that backend.

---

# arc-cua

**Superfast action layer for computer-use agents, powered by decision models.**

Ships with a lean macOS driver you can use on its own, without the rest of arc-cua: see [docs/driver.md](https://github.com/shhivv/arc-cua/blob/master/docs/driver.md).

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

## Two layers: use either

- **A macOS driver.** It reads an app's window, runs its menu commands and acts on
  its controls in the background, so the user's pointer, front app and windows
  stay as they are. It checks each action against the app as it is when the action
  runs, and works in minimized windows and hidden apps. Use it on its own, with no
  decision model: from any MCP client with `arc-cua mcp`, or from Python with
  `arc_cua.Driver`. See **[docs/driver.md](https://github.com/shhivv/arc-cua/blob/master/docs/driver.md)**.
- **An action layer** on top of it: hand a bounded subtask to a fast decision
  model, which runs the UI loop and returns when the subtask is done. The rest of
  this README is about this layer.

```bash
claude mcp add arc-cua -- uvx --from 'arc-cua[macos]' arc-cua mcp   # the driver as MCP tools in Claude Code
```

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
SUBTASK_COMPLETE / BLOCKED / NEEDS_AGENT / NEEDS_INPUT
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

An answer that fails validation, such as a choice outside the offered options,
executes nothing. `ChoicePolicy` asks the same questions again (`invalid_retries`,
default 1), then raises `InvalidChoiceResponse` naming the question.

See [the extension guide](https://github.com/shhivv/arc-cua/blob/master/site/llms-full.txt) for the request and answer shapes.

### The agent owns intent

The upstream agent decides what needs to happen, what literal text may be used, what must not happen, and what counts as success. JEV chooses which element to target and which operation to perform — but never invents arbitrary text. Literal values always originate from the agent via `inputs`.

When a field needs a value that none of the `inputs` provides, the run stops with `NEEDS_INPUT` instead of typing something else. The result's `needs_input` describes the field (`element_id`, `role`, `name`, current `value`, and a dropdown's `options`), so the agent can add the value to `inputs` and run the subtask again. Text fields are offered even when no inputs are supplied, only so the model can ask this way.

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

Chords use uppercase key names and one or more `MOD`, `CTRL`, `ALT`, or `SHIFT` modifiers, for example `MOD+S`, `CTRL+ALT+7`, or `SHIFT+F12`. `MOD` means Command on macOS. Supported keys include A-Z, 0-9, F1-F20, navigation keys, and named punctuation keys; see [the keyboard vocabulary](https://github.com/shhivv/arc-cua/blob/master/src/arc_cua/keyboard.py). The macOS backend uses US/ANSI physical key positions. Each shortcut is one chord, not a sequence of actions.

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
| `allowed_risks` | Array of `"delete"`, `"send"`, `"purchase"`, `"close"`; defaults to `[]` |
| `secret_inputs` | Array of `inputs` keys whose values the model never sees; defaults to `[]` |

For example, use `"verification": ["The folder exists"]`, even for one criterion.
A bare string is rejected rather than split into characters. The Python API accepts
lists or tuples for criteria and constraints, and copies them into tuples. Input
literals are copied into an immutable mapping. Invalid values raise a field-specific
`ValueError`; values are not silently converted from strings to numbers or arrays.

Supply each folder name, filename, or path to be typed as a literal in `inputs`.
Text mentioned only in `goal` cannot be invented as an input by JEV.
Use `MOD+SHIFT+N`, not `Command+Shift+N`; an unmodified Return is the built-in
`PRESS_KEY` value `ENTER`, not an extra hotkey.

### Risky controls and secrets

Controls whose label reads like a consequential action are not offered to the
decision model unless the subtask allows that category in `allowed_risks`:

| Category | Label words (whole words, any case) |
|---|---|
| `delete` | delete, remove, erase, trash, discard, clear all, empty trash, permanently |
| `send` | send, post, publish, share, reply all, forward, tweet |
| `purchase` | buy, purchase, pay, checkout, check out, place order, order now, subscribe, donate, confirm payment |
| `close` | close, quit, exit, sign out, log out, logout, shut down, restart, uninstall |

This applies to `CLICK` and `DOUBLE_CLICK`. The runtime also refuses such a click
from any policy and returns `NEEDS_AGENT` with the reason, without acting.

Values named in `secret_inputs` are replaced by `[secret]` everywhere the decision
model reads them: the subtask, the input choices, observed element text, and the
action history. The runtime still enters the real value. `result_to_dict`, the
`arc-cua run` output and the runtime's error log are redacted the same way.
Screenshots sent with `screenshot_checks` or `screenshot_steps` are not redacted.

```python
Subtask(
    goal="Sign in to the account",
    inputs={"email": "sam@example.com", "password": "..."},
    secret_inputs=("password",),
    verification=("The account page is shown",),
)
```

### Hybrid macOS perception

`arc-cua` combines two local perception sources:

- **Accessibility (AX)** — semantic controls: buttons, fields, menus, roles, values, native actions
- **Apple Vision OCR** — visible screen text with bounding boxes, for apps with incomplete accessibility

Both normalize into `DesktopElement`s that JEV reasons over. `MacOSHybridBackend` runs OCR only when needed (`ocr="auto"`, the default): when accessibility exposes no enabled, labelled control of the app itself (title-bar buttons, the window and unlabelled groups do not count), as in Spotify, it adds OCR text; otherwise it takes no screenshot and settles on accessibility notifications. OCR uses Apple Vision's accurate level by default (`ocr_recognition_level="accurate"`); in Spotify it read "Sneaky Snitch" where the fast level read "Sn8aky SNitch", at about 95 ms per observation against 30 ms. `ocr="always"` and `ocr="never"` force either path, and `snapshot.context["perception_sources"]` records which one ran. On a Clock alarm task, `auto` took 2.8–3.0 s against 3.6 s with `always`. JEV receives structured elements and IDs, not screenshots. Providers that accept images can also receive a window screenshot for completion checks; see [Terminal states](#terminal-states).

The provider request stores element facts once in a shared table. Target choices
refer to those observed IDs, and all questions share the same subtask. This reduces
repeated request data without discarding element facts or changing the offered choices.
Provider token-limit failures include `max_tokens_exceeded` in the returned error;
no UI action is executed for a failed decision request.

**Accessibility only.** For apps that describe themselves well to accessibility,
`MacOSAXBackend` needs no screenshots, OCR or Screen Recording permission: it
settles on the app's accessibility notifications instead of screen thumbnails. The
walk reads only what is on screen, meaning the visible rows of lists and tables and
elements inside the window and its scroll areas, so a Finder list of 2,000 files
observes in about 50 ms. Electron and other Chromium-based apps are asked for their
full accessibility tree. `MacOSAXBackend(pid, cache=True)` also keeps elements
between observations and re-reads only what the app reports as changed, so repeat
observations take a few milliseconds; values an app changes without notifying can
be briefly stale, which is why it is opt-in. `MacOSHybridBackend` adds OCR
automatically for apps with little accessibility.

### Background control on macOS

`MacOSHybridBackend` and `MacOSAXBackend` act on one app, given by its process ID,
whether or not it is frontmost. Input reaches that app in the background: clicks,
keys, text and scrolling are addressed to its window, so the user's pointer does not
move, the window is not raised and the front app does not change. The user can keep
working while a subtask runs.

```python
from arc_cua import DesktopExecutor
from arc_cua.backends import MacOSApp, MacOSHybridBackend
from arc_cua.policies import TypeSafeJevPolicy

pid = MacOSApp.from_bundle_id("com.apple.TextEdit").pid
with MacOSHybridBackend(pid) as backend:
    result = DesktopExecutor(backend, TypeSafeJevPolicy()).run(subtask)
```

- **Accessibility first.** `AXPress` and `AXValue` need no events at all. Other
  input is posted to the app's process, with key focus lent to its window for the
  few milliseconds an event batch takes and then handed back to the user's window.
- **Command shortcuts** go through the app's menu when an enabled menu item has
  them, since menu key equivalents only reach the front app. `MOD+A` in a text
  field selects its text through accessibility.
- **Hidden and minimized windows.** Used as a context manager (or with `open()` and
  `close()`), `MacOSAXBackend` reads a minimized window or a hidden app as it is and
  presses and sets its controls through accessibility, so the window stays where it
  is. An action that needs input events (keys, scrolling, pointer clicks) first moves
  the window onto an invisible display where the app renders it and takes input; on
  exit it is minimized or hidden again and moved back. `MacOSHybridBackend` needs
  pixels for OCR, so it moves the window when it opens.
- **If the app activates itself** after an input, which some controls do, the
  user's app is brought back to the front.
- **Clear failures.** Once the app quits, or has no usable window (closed, or on
  another desktop), observing or acting raises `TargetUnavailable` with the reason.
  The runtime does not retry it.

### Command line

`arc-cua run` executes one subtask against one macOS app and exits. It reads one
JSON object from standard input:

```json
{
  "app": {"pid": 4242},
  "subtask": {
    "goal": "Replace the document's text with the supplied text",
    "inputs": {"text": "Hello"},
    "verification": ["The document's text is exactly: Hello"],
    "max_actions": 10
  },
  "provider": {"name": "jev", "api_key": "..."}
}
```

| Field | Meaning |
|---|---|
| `app` | `{"pid": ...}`, or `{"bundle_id": "com.apple.TextEdit"}` for the first running instance (required) |
| `subtask` | The subtask, with the fields in [JSON field types](#json-field-types) (required) |
| `provider` | `name` (`"jev"`), `api_key`, and optionally `model` (required) |
| `backend` | `"hybrid"` (AX, with OCR when accessibility exposes no app controls; the default) or `"ax"` |
| `timeout_s`, `min_confidence`, `min_margin` | As in `RuntimeConfig` |
| `dry_run` | `true` to decide and validate the next action, then stop with `DRY_RUN` without acting |

It prints one JSON line to standard output after every action, then a final line,
and exits:

```text
{"type": "action", "step": 1, "action": "SET_VALUE", "target": "ax_3", "target_name": "", "value": "Hello", "state_changed": true, "confidence": 0.48, ...}
{"type": "result", "status": "SUBTASK_COMPLETE", "reason": null, "needs_input": null, "actions_taken": 1, "application": "TextEdit", "window": "Untitled", ...}
```

Action lines carry the same fields as a history record in `result_to_dict`. The
exit code is 0 after a result line, whatever its status. When the run cannot
produce a result, the last line is `{"type": "error", "error": "..."}` instead:
exit code 2 for invalid input, 1 when the app quit or has no usable window, a
permission is missing, or the provider failed. Logs go to standard error, never
standard output (`-v` for debug detail).

`--log FILE` appends one JSON line per decision to `FILE`, including the final
one: `step`, `choice`, `target`, `target_name`, `input_key`, `confidence`, `margin`, `decide_ms`, `step_elapsed_ms`, `state_changed`, `candidate_counts` (options per question), `operation_probabilities` and `outcome`. Secret inputs are redacted there too.

Runs are stateless: each process runs one subtask. To stop a run, terminate the
process (`SIGTERM` or `SIGINT`); it exits after putting back any windows it moved
out of sight and returning key focus.

### Browser (Chrome)

`ChromeBackend` runs the same loop in a Chrome tab, on macOS, Linux and Windows. It
reads the page's DOM in one script call and acts through the Chrome DevTools
protocol, so the real pointer and keyboard focus are untouched and the window can
stay in the background.

```python
from arc_cua import DesktopExecutor, Subtask
from arc_cua.backends import ChromeBackend
from arc_cua.policies import TypeSafeJevPolicy

with ChromeBackend.launch("https://en.wikipedia.org") as backend:
    result = DesktopExecutor(backend, TypeSafeJevPolicy()).run(Subtask(
        goal="Open the Wikipedia article about Gödel's incompleteness theorems",
        inputs={"query": "Gödel's incompleteness theorems"},
        verification=("The article titled Gödel's incompleteness theorems is open",),
    ))
```

- **Elements** are the controls and text visible in the viewport, with roles,
  accessible names, values and states. Open shadow roots and same-origin iframes
  are included. `SCROLL` reveals more; `context["more_below"]` says whether there is more.
- **Covered controls**, such as a button behind a cookie banner, are listed without
  actions until they can actually receive a click. A radio button that is already
  checked offers no click either, since clicking it changes nothing.
- **Custom widgets** without semantics are found by their pointer cursor. A wrapper
  around a real control, such as a styled radio button, is not listed separately;
  the control inside it is.
- **Settling** waits for the document, XHR and fetch requests an action started to
  finish, as well as for the DOM to go quiet, so results loaded after a click are
  observed. Requests already open before the action (long polling, streams) do not
  hold it up.
- **Native dropdowns, sliders and date inputs** use `SET_VALUE` with an
  agent-supplied input. A `<select>` lists its options in `metadata["options"]`.
- **Status messages** in live regions (`role=status`, `role=alert`, `aria-live`)
  are observed even when scrolled out of view, as a screen reader would announce
  them, with `metadata["offscreen"] = true`. Completion checks can rely on them.
- **JavaScript dialogs** (`alert`, `confirm`) appear as a dialog with OK and
  Cancel buttons. Links that open a new tab switch the backend to that tab.
- **Speed.** Observing a 4,761-node Wikipedia article takes about 9 ms, and the
  settle probe about 0.5 ms. See [Provider comparison](#provider-comparison) for
  end-to-end task times.

`ChromeBackend.launch()` starts Chrome with a temporary profile; `connect()` opens a
new tab in a Chrome started with `--remote-debugging-port`. `navigate(url)` is for
the caller; the decision model has no address bar. Cross-origin iframes, closed
shadow roots, file uploads, drag and drop, and browser UI (address bar, find bar,
extensions) are not reachable.

### Runtime-owned settling

After a mutating action, `arc-cua` waits until the desktop has reacted and gone quiet, then observes it once. The decision model decides **what to do**; the runtime decides **when the UI is ready to reason over again**.

The hybrid macOS backend settles on a cheap visual probe: the target app's front window plus a small grayscale thumbnail of the app's own windows in that area (sheets and panels included; other apps' windows covering it are not). The runtime waits up to `settle_reaction_s` (0.6 s; covers an app still busy with the previous action) for a visible reaction, then until the probe has been unchanged for `settle_quiet_s` (0.15 s), capped at `settle_timeout_s` (2 s). A caret-sized change does not count as activity. Full AX + OCR observations are not used for settling because OCR output varies slightly between passes even when the UI is identical. The accessibility-only `MacOSAXBackend` settles on the app's accessibility notification count instead, which needs no screen capture or Screen Recording permission; on a Clock alarm task this cut the time spent waiting from about 1.3 s to 0.75 s. `ChromeBackend` also waits for the requests an action started, and raises the cap to 10 s while they last (Google Flights' booking page fetches its prices in one request that took 3–7 s) (a backend's `settle_timeout_s` can raise the configured cap, never lower it). Backends without a `settle_probe()` method keep snapshot-based settling.

Some apps react, pause while they work, then show the result: Finder's New Folder with Selection redraws within 0.06 s, posts nothing for about 0.8 s while it moves the files, then shows the new folder. Settling ends in the pause. So when the policy answers `NEEDS_AGENT` or `BLOCKED` right after an action, the runtime waits `late_reaction_s` (1 s), observes again and, if the desktop changed, asks the policy again. This happens once per action and costs nothing on the normal path.

`TYPE_TEXT` can press `ENTER` or `TAB` right after entering its value (`Decision.key`), so a path, search or name field can be filled and submitted in one decision. The runtime waits for the typed value to settle before pressing the key, and the history records the key with the `TYPE_TEXT` action.

`CLICK` can hold a selection modifier (`Decision.click_modifier`): `MOD` (Cmd on macOS) adds the target to or removes it from the current selection, and `SHIFT` extends a range to it. This lets one subtask select several specific items, for example files to copy or move together. On macOS a modified click is always a mouse event at the element's center, addressed to the app's window, because `AXPress` ignores modifiers.

### Terminal states

| Status | Meaning |
|---|---|
| `SUBTASK_COMPLETE` | Verification criteria appear satisfied |
| `BLOCKED` | Cannot make progress with available operations |
| `NEEDS_AGENT` | Higher-level reasoning required or action budget reached |
| `NEEDS_INPUT` | A field needs a value none of the `inputs` provides; `needs_input` names the field |
| `DRY_RUN` | Dry run: the next action was chosen and validated but not performed; `planned_action` describes it |

The caller owns overall task completion.

The JEV policy checks each supplied verification criterion in a separate choice
head. A proposed completion becomes `NEEDS_AGENT` with a reason if any criterion
is contradicted or cannot be established. These checks are model judgements;
use `RuntimeConfig.verify` or caller-side validation when completion needs an
independent check.

**Screenshot completion checks.** With a provider that accepts images, use
`ChoicePolicy(transport, screenshot_checks=True)`. When the model proposes
`SUBTASK_COMPLETE`, the policy asks the verification questions again with a PNG
attached; those answers decide completion. The image is the one the snapshot's
elements were read from (`DesktopSnapshot.screenshot`), not a later capture, so it
cannot show a different state than the model judged. Ordinary steps send no image,
so only a completion costs an extra request. On macOS it is the window image OCR
read, scaled to at most 1280 px on the longest side and encoded only when needed.
JEV does not accept images, so `TypeSafeJevPolicy` does not offer this.

`ChoicePolicy(transport, screenshot_steps=True)` goes further and attaches the
snapshot's screenshot to every decision, for interfaces whose structure says little
(canvases, charts, custom-drawn controls). The model still chooses only offered ids.
Completion checks then already see the image, so no separate request is made.
`ChromeBackend(capture_screenshots=True)` provides the viewport image; the macOS
backend always does.

**Confidence and margin thresholds.** `RuntimeConfig(min_confidence=0.6)` returns
`NEEDS_AGENT` instead of acting, or completing, when a decision's confidence is
below the threshold. `RuntimeConfig(min_margin=0.1)` does the same for near-ties:
when the chosen option's probability leads the runner-up's by less than the margin.
A `ChoicePolicy` decision's confidence and margin are those of the weakest answer it
uses (operation, target, input, key, completion checks). `BLOCKED` and
`NEEDS_AGENT` are never gated, and decisions that don't report a value are not gated.

---

## Install

The desktop backends are macOS-only; the Chrome backend runs wherever Chrome does.

```bash
pip install 'arc-cua[macos]'    # macOS desktop apps (the driver needs this extra)
pip install 'arc-cua[browser]'  # Chrome
```

From a checkout: `pip install -e '.[macos]'`.

To use only the driver, that is all: run `arc-cua mcp` from your MCP client, or
use `arc_cua.Driver` (see [docs/driver.md](https://github.com/shhivv/arc-cua/blob/master/docs/driver.md)). For the action layer,
set your TypeSafe key:

```bash
export TYPESAFE_API_KEY=...
```

### macOS permissions

The terminal/editor running Python needs both:

- **Accessibility** — System Settings → Privacy & Security → Accessibility
- **Screen Recording** — System Settings → Privacy & Security → Screen Recording (required for OCR and screenshot completion checks)

Restart the terminal after granting permissions if necessary.

---

## Benchmarks

`benchmarks/` measures the driver on real apps and realistic multi-step workflows,
and compares runs between versions: `python benchmarks/run.py all`, then
`python benchmarks/run.py compare OLD.json NEW.json`. See
[benchmarks/README.md](https://github.com/shhivv/arc-cua/blob/master/benchmarks/README.md).

## Examples

### Deterministic architecture demo

No API key required:

```bash
python examples/effects_demo.py
```

### macOS probes

```bash
python examples/macos_ax_probe.py com.apple.TextEdit   # Inspect an app's AX tree (bundle ID or pid)
python examples/ocr_probe.py com.apple.TextEdit        # Inspect visible text via Apple Vision
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

### Provider comparison

`examples/compare_providers.py` runs the same browser tasks with every decision
provider whose key is set and judges success from the page itself, not from the
model's completion claim. JEV, 5 runs each, headless Chrome on an M-series Mac:

| Task | Verified | Median time | Decisions | Median decision latency |
|---|---|---|---|---|
| Trip form: type a city, pick a cabin, tick a hidden checkbox, search (local) | 5/5 | 2.9 s | 5 | 273 ms |
| Covered button: scroll, dismiss a cookie banner, click the button beneath (local) | 5/5 | 2.3 s | 5 | 306 ms |
| Wikipedia: search and open an article (live site) | 5/5 | 2.0 s | 2 | 319 ms |

```bash
python examples/compare_providers.py --runs 5
```

### Browser

Run any subtask in Chrome:

```bash
python examples/browser_demo.py https://en.wikipedia.org \
  "Open the Wikipedia article about Gödel's incompleteness theorems" \
  --input "query=Gödel's incompleteness theorems" \
  --verify "The article titled Gödel's incompleteness theorems is open"
```

---

## Roadmap

AX + OCR covers native and Electron desktop workflows. The next perception frontier is custom graphical interfaces — video timelines, CAD canvases, node graphs, spatial drag targets — which can be added as perception providers while keeping the same `DesktopElement` and execution interfaces.
