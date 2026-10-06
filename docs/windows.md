# arc-cua Windows MVP preview

This is a public MIT fork of [Shiv Shanmugam's arc-cua](https://github.com/shhivv/arc-cua), adding a **foreground Windows UI Automation backend**. The upstream MIT license and author attribution are preserved. The macOS and Chrome backends remain available.

This preview runs on a Windows desktop. A public download is not a cloud-hosted Windows machine: it cannot control a visitor's PC through a webpage. Download and run locally, or on your own Windows VM with an unlocked interactive desktop. No remote desktop access service is exposed.

## Easiest setup: downloadable preview

1. Use Windows 10/11 x64. Install **Python 3.12 x64** from python.org, including the Python launcher (`py`).
2. Download `arc-cua-windows-preview.zip` from the [corrected preview release](https://github.com/GreenKeewi/arc-cua-windows/releases/tag/windows-preview-1a72de79831e). The CI release bundles the project wheel and Windows dependency wheels, not a Python runtime or standalone EXE.
3. Extract the ZIP fully into a folder you can write to. Open `install.cmd`. It creates a local virtual environment and installs from the bundled wheels, without downloading packages.
4. Leave the desktop unlocked and stop moving the mouse/typing while the demo runs. Open `demo.cmd` in Command Prompt to see its output.
5. A disposable form appears. The demo enters `Hello from arc-cua + {literal} café`, performs Ctrl+A and End, scrolls its list, clicks **Apply message**, and checks the result label. It closes the form afterward. No model, key, network access, personal files or other applications are involved.

Success prints `"live_windows_validated": true`, five action kinds and `SUBTASK_COMPLETE`. That proves behavior only on this fixture, on the machine where you ran it. A failure prints its reason and returns a nonzero exit code. If PowerShell policy blocks the bundled fixture, follow your machine's policy; don't disable organizational policy. You can still use the backend on your own open app.

The dependency bundle is built for the CI Windows x64/Python 3.12 environment. On ARM64 or another Python version, use the source installation below and resolve dependencies for your machine.

## Setup from source

In PowerShell, with Git and Python 3.12 installed:

```powershell
git clone https://github.com/GreenKeewi/arc-cua-windows.git
cd arc-cua-windows
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[windows,browser]"
.\.venv\Scripts\python.exe -m arc_cua windows-smoke
```

No activation or system-wide execution policy change is necessary. Run all subsequent commands using this environment's Python. Source installation needs internet access. The ZIP installer does not.

## Observe and act without a model

Find the target app's PID in Task Manager's Details tab. Keep its window visible and restored. This Python example uses the exact window handle selected by the backend:

```python
from arc_cua.backends import WindowsUIABackend
from arc_cua.models import ActionKind, ExecutableAction

backend = WindowsUIABackend(1234)  # replace with the target PID
snapshot = backend.observe()
for element in snapshot.elements:
    if element.visible:
        print(element.id, element.role, element.name, element.actions)
# Use an ID from that observation, never invent one:
field = next(e for e in snapshot.elements if e.name == "Search"
             and ActionKind.TYPE_TEXT in e.actions)
backend.execute(snapshot, ExecutableAction(
    kind=ActionKind.TYPE_TEXT, target_id=field.id,
    target_guard=field.semantic_guard(), value="hello",
))
backend.close()
```

A stale snapshot raises `StaleDesktopState`; observe again and reconsider the action. Activation can itself change focus and invalidate a snapshot. No action is sent in that case. The decision loop automatically retries stale decisions.

Supported: click/double-click/right-click on visible UIA controls; replacement text entry on editable fields; ValuePattern set; keys and single keyboard chords; UIA ScrollPattern scrolling; wait. `MOD` means **Ctrl**, not the Windows key. UIA role `Edit` is normalized to `TextField`. `TYPE_TEXT` replaces the entire field (Ctrl+A then escaped literal Unicode text); `SET_VALUE` uses ValuePattern. Text is not interpreted as a keyboard macro. Physical keys and punctuation depend on the installed keyboard layout.

For scrolling, MCP can name a control offering `SCROLL`. The existing choice policy supplies just a direction; the backend selects the focused control's scrollable ancestor, otherwise the first visible scrollable control. If no such control exists, scrolling returns an unsupported-action error.

## MCP setup

Configure your MCP client with an absolute Python path. This is a local stdio server, not an HTTP service:

```json
{
  "mcpServers": {
    "arc-cua-windows": {
      "command": "C:\\path\\arc-cua-windows\\.venv\\Scripts\\python.exe",
      "args": ["-m", "arc_cua", "mcp"]
    }
  }
}
```

Tools on Windows: `status`, `apps`, `windows`, `observe`, `act`, `wait`, `settle`, `release`. Call `apps` to find a PID, `windows` for exact HWNDs, and `observe` for a snapshot and named controls. Call `act` with that snapshot ID and an offered action/control. `stale` returns a fresh snapshot; decide again before retrying. `release` expires cached snapshots. Cancellation is cooperative between calls; an in-flight UIA/COM call cannot be interrupted.

Example arguments (replace IDs with returned ones):

```json
{"pid":1234,"window_id":5678}
{"snapshot":"s1","action":"TYPE_TEXT","element":"uia:42:9","value":"hello","settle":true}
```

Password Value/Text patterns and password control names are excluded from observations. Other field values are visible to callers and, when using a policy, its provider. Upstream `secret_inputs` redaction remains available for supplied secrets. Don't store keys or sensitive task payloads in source control.

## Optional decision provider

The existing `DesktopExecutor` loop works with `WindowsUIABackend`, including action validation, bounded action counts, stale retries, risk checks, cancellation and verification callbacks. The smoke demo uses a deterministic policy and needs no provider.

For the existing JEV provider, obtain your own TypeSafe credentials and set them **only in the local process environment**:

```powershell
$key = Read-Host 'Your TypeSafe API key' -AsSecureString
$env:TYPESAFE_API_KEY = [System.Net.NetworkCredential]::new('', $key).Password
Remove-Variable key
@'
{
  "app":{"pid":1234},
  "backend":"windows",
  "provider":{"name":"jev"},
  "subtask":{
    "goal":"Fill the Search field with hello",
    "inputs":{"message":"hello"},
    "verification":["The Search field contains hello"],
    "max_actions":8
  },
  "timeout_s":30
}
'@ | .\.venv\Scripts\python.exe -m arc_cua run
Remove-Item Env:TYPESAFE_API_KEY
```

Replace PID/goal/field criteria with your target. `TYPESAFE_MODEL` can override the upstream default. Model completion is provider-reported unless you supply a Python verification callback; it is not a guarantee the intended work happened. Provider availability, billing and credentials are supplied by you; no live provider was used to validate this preview.

## Limitations and unsupported controls

- Requires foreground interaction, an unlocked interactive desktop, and restored visible windows. Focus and the real pointer can move. Background/minimized/locked-desktop automation is not supported.
- UAC secure desktop, elevated applications from an unelevated process, protected windows, games, canvas/custom-painted controls without UIA, and many virtualized/offscreen controls may be absent or unusable. This preview does not bypass permission boundaries.
- No Windows OCR, screenshots, coordinate actions, drag-and-drop, menu-path enumeration or virtual display. OCR was deferred because this cloud environment cannot validate a Windows OCR pipeline; controls absent from UIA cannot be inferred safely here.
- Observation limits traversal to 1,500 controls and depth 40. `truncated` and `read_errors` diagnostics are recorded in Python snapshot context. A disappearing/inaccessible control is omitted. COM provider calls are synchronous; a hung provider can exceed runtime/settle timeouts. Timeouts bound the decision loop between calls, not native COM calls.
- Strict full-tree revisions include names, values, enabled/visible/focused state and bounds. Frequently changing apps can cause stale retries and hand back to the caller. There is still a small unavoidable race between revalidation and physical input; don't interact concurrently.
- Chrome's DOM/CDP backend is preserved, with its existing launch/connect Python API and `[browser]` extra. Windows CLI `run` addresses UIA apps; Chrome isn't a CLI backend in upstream and remains a separate Python API.

## Validation and manual checks

`tests/test_windows_uia.py` uses a fake native boundary for backend/runtime/CLI/MCP contracts and fake wrappers for the native adapter. These tests run on Linux and Windows; **they do not prove live Windows behavior**. Upstream tests that explicitly import macOS frameworks are skipped on other operating systems. Other upstream tests, including Chrome unit tests, remain in the suite.

CI validates Python 3.12/3.13 on Linux and Windows, builds wheel/source distributions, creates an offline preview ZIP, and publishes a public prerelease on pushes to this fork. It also attempts the live disposable fixture on a hosted Windows runner. Its JSON is uploaded as `hosted-windows-smoke-evidence`; a failed hosted attempt does not block packaging. Read that artifact to distinguish a successful fixture run from a runner lacking interactive focus. Even a passing hosted fixture does not validate general applications.

Before relying on this preview, manually check on your Windows machine:

- `demo.cmd` completes with the exact Unicode/literal message and all five actions.
- Notepad/editor UIA names and typing; calculator buttons, double/right clicks where offered.
- Vertical/horizontal scrolling on controls that expose ScrollPattern; Ctrl shortcuts, keyboard layouts.
- Multiple windows in one PID, app closure, disabled/disappearing controls, stale snapshots and foreground refusal.
- 100%, 150% and 200% DPI, multiple monitors; visible restored versus minimized/locked/elevated windows.
- A real MCP client observe/act/stale/release/cancel sequence; optional provider using your own key.

Preserved upstream revision at the start of this work: `6ca19d62c95106732fad28f488ecd458c08e02f4`. See `docs/windows-validation.md` for recorded cloud results.
