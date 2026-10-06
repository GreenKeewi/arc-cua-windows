# Windows preview validation evidence

## Cloud/Linux checks before publication

Environment: Linux, Python 3.12.14. Upstream base: `6ca19d62c95106732fad28f488ecd458c08e02f4`.

- Full portable suite: **393 passed, 10 skipped**, 25.95 seconds (`python -m pytest -q`). Eight expanded upstream cases require macOS AppKit/ApplicationServices, the upstream live Chrome module is skipped because Chrome is absent, and one new native Windows dependency test is skipped on Linux.
- Windows backend/CLI/MCP/action-validation integration selection: **89 passed, 1 skipped**, 3.83 seconds.
- Ruff lint of changed implementation and new tests: passed.
- `git diff --check`: passed.
- Wheel and source distribution built successfully: `arc_cua-0.1.2.dev1-py3-none-any.whl` and `arc_cua-0.1.2.dev1.tar.gz`.
- Linux `python -m arc_cua windows-smoke`: exit 2, `unsupported_platform`, `live_windows_validated: false`, as intended.
- No live provider calls or user credentials were used. Test credentials are dummy values from upstream tests. License unchanged.

An initial unrestricted suite attempt encountered eight expanded macOS-framework cases and HTTPX's missing SOCKS support for this cloud environment's proxy. The cloud test environment was given `socksio`; the repository does not require that environment-specific dependency. Explicit non-macOS test exclusions document the framework cases, rather than pretending they passed.

## What these checks establish

Tests cover observation normalization, exact window identity, changed bounds/value/name/focus/visibility/enabled state, stale rejection before input, revalidation after activation, unsupported-action refusal, guard checks, keyboard literal escaping, Ctrl mapping, scroll target preservation, native adapter pattern mapping with fake wrappers, password observation suppression, traversal limits, existing runtime verification, CLI routing, Windows signal compatibility, MCP capability filtering, stale refresh and release.

The native adapter is mocked in Linux checks. **These results do not validate real Windows controls, focus, clicks, key events, Unicode input or scrolling.** The initial cloud workspace had no Chrome; its live Chrome module was skipped. GitHub runners did have Chrome and subsequently ran all 14 live headless Chrome fixture tests.

## Windows CI and downloads

[The workflow](https://github.com/GreenKeewi/arc-cua-windows/actions/workflows/windows-preview.yml) runs the suite on Windows and Linux, Python 3.12/3.13, and builds distributions. A Windows job bundles native dependencies for a Python 3.12/x64 offline installer ZIP. Successful pushes to this fork publish it as a public prerelease.

A separate step attempts `windows-smoke` on the hosted runner and publishes its JSON as `hosted-windows-smoke-evidence`. Inspect the specific run's output: only `live_windows_validated: true` is evidence of a live **fixture** success. This attempt may fail when an interactive foreground desktop isn't usable. Package publication is intentionally independent of the fixture attempt; unit/build success must not be described as desktop success.

First public implementation: `b65a9a523fc84a37959d6ea853e29efe314110f6`.
[CI run 37396034360](https://github.com/GreenKeewi/arc-cua-windows/actions/runs/37396034360) passed all four contract/lint/build jobs:

| Runner | Python | Passed | Skipped |
|---|---|---:|---:|
| Windows | 3.12 | 408 | 8 macOS framework cases |
| Windows | 3.13 | 408 | 8 macOS framework cases |
| Linux | 3.12 | 407 | 8 macOS cases + Windows native import |
| Linux | 3.13 | 407 | 8 macOS cases + Windows native import |

The Windows package job published a ZIP, but its separate live fixture attempt returned `NEEDS_AGENT` / `live_windows_validated: false`: all five actions were dispatched, and the exact result text verification failed. This is **not** evidence of successful end-to-end Windows control. Follow-up inspection found shortcut letter keys were sent as Unicode packets; the subsequent fix sends physical key/shortcut sequences with `vk_packet=False` and adds fixture-only diagnostics. A rerun is required to establish whether this resolves the failure.

Windows local manual checks are listed in [windows.md](windows.md).


## Corrected preview: hosted Windows fixture passed

Implementation/fix commit: `1a72de79831e94ef263848cc4d0c2d19e8dd7338`.
[CI run 37396427697](https://github.com/GreenKeewi/arc-cua-windows/actions/runs/37396427697) completed successfully, including contract tests, lint, builds, bundling and prerelease publication.

| Runner | Python | Passed | Skipped |
|---|---|---:|---:|
| Windows | 3.12 | 409 | 8 macOS framework cases |
| Windows | 3.13 | 409 | 8 macOS framework cases |
| Linux | 3.12 | 408 | 8 macOS cases + Windows native import |
| Linux | 3.13 | 408 | 8 macOS cases + Windows native import |

The extra 14 tests compared with this workspace are upstream live headless Chrome fixture tests: Chrome was installed on the GitHub runners. This provides live Chrome regression evidence on both Windows and Linux. The corrected local Linux suite reported **394 passed, 10 skipped** in 26.06 seconds; the focused backend/CLI/MCP/validation selection reported **90 passed, 1 skipped** in 3.86 seconds.

The hosted native smoke ran on **Microsoft Windows Server 2025, 10.0.26100, x64, Python 3.12**. Its actual JSON reported:

```json
{
  "status": "SUBTASK_COMPLETE",
  "live_windows_validated": true,
  "actions": ["TYPE_TEXT", "HOTKEY", "PRESS_KEY", "SCROLL", "CLICK"],
  "reason": null
}
```

Its final observed `Message` value was `Hello from arc-cua + {literal} café`, and the result label was exactly `Received: Hello from arc-cua + {literal} café`. The demo's verification callback checked that exact label. This demonstrates the foreground/native path end-to-end **on this disposable WinForms fixture only**. It does not establish behavior on all Windows controls, verify every shortcut's effect independently, or replace Windows 10/11 manual checks. The full JSON is retained in the run's `hosted-windows-smoke-evidence` artifact and package job logs.

**Use the corrected preview**, not the first preview whose smoke failed:

- [Public corrected release](https://github.com/GreenKeewi/arc-cua-windows/releases/tag/windows-preview-1a72de79831e)
- [Download arc-cua-windows-preview.zip](https://github.com/GreenKeewi/arc-cua-windows/releases/download/windows-preview-1a72de79831e/arc-cua-windows-preview.zip)
- ZIP size: 8,549,512 bytes.
- SHA256: `1eaa054c00e74ae2051dc06b83c5127175a830cd75041bb5089650d4129bfb9a`.

The corrected ZIP was also downloaded without authentication and verified locally against its published SHA256, commit stamp and every bundled wheel hash. Its wheel contains the shortcut fix and fixture; its MIT license content matches upstream (Windows packaging uses CRLF line endings). No .env or virtual environment files are included.

No model/provider credentials were used in any of these runs. General Windows 10/11 app compatibility, DPI, multiple monitors, non-US layouts, minimized/locked/elevated refusal, and an actual MCP client's round trip remain manual checks. Source attribution and MIT license remain unchanged.
