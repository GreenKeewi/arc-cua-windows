# Finder filename-sorting demo

Two fresh Codex tasks organize identical folders of 18 fictional PDFs in Finder.
All classification information is visible in filenames. One performs the UI work
directly; the other delegates bounded folder creation and file moves to arc-cua.
Neither task opens or reads the PDF contents.

The corpus has three clients, three document types, and two documents of each type
per client. Starting names are `YYYY-MM-DD - Client - Reference.pdf`; INV, PROP and
MTG references map to Invoices, Proposals and Meeting Notes. Preserve the names.
The earlier document-reading workflow is retained with `--workflow preview`.

The library's default action space is unchanged. The Codex caller can declare
shortcuts in each subtask using the public `shortcuts` field. The handoff helper
contains no predetermined click sequence or document classification logic.

## Setup

Use Python 3.12+ with the project installed, macOS dependencies, Accessibility,
and Screen Recording permission. Fixture creation also needs `reportlab`.
The live arc-cua run requires `TYPESAFE_API_KEY` in the execution environment or
the repository's ignored `.env` file. The helper accepts quoted values and an
optional `export` prefix; it does not execute environment files as shell code.

```sh
python examples/finder_preview/prepare.py --output /path/to/demo/corpus
python examples/finder_preview/benchmark.py setup direct-files-01 --mode direct --workflow filenames --root /path/to/demo --corpus /path/to/demo/corpus
python examples/finder_preview/benchmark.py setup arc-files-01 --mode arc --workflow filenames --root /path/to/demo --corpus /path/to/demo/corpus
```

Each trial gets its own `workspace/Inbox` and `task.md`. Existing trials are never
overwritten. Create another trial name for a reset. The manifest stays outside
the workspace and is reserved for setup and evaluation.

Before measuring, use a separate `--rehearsal --workflow filenames --count 18`
trial to check permissions, handoffs, and request size with the full file list.
Rehearsals are excluded from the comparison summary.

## Run in Codex

Use a fresh Codex task for each run, with the same model and reasoning settings.
This preparation task has seen the answer key and should only perform rehearsals.
Give each participant only its generated `task.md` prompt. Do not give the corpus,
generator, expected destinations, or the other task's results to either participant.
Run them sequentially because they share the same desktop.

For each run, show its Inbox in Finder list view, with the same window geometry
and no trial document open. Start recording before the agent begins.
Close leftover rehearsal tabs before each run.
The prompt includes start and finish commands that time the whole workflow.
The same shortcuts and ordinary computer-use batching are allowed on both sides.

The shared task is:

> Using filenames only, create `Organized/Client/Document Type` folders and move
> each file into the correct destination. Keep filenames and contents unchanged,
> preserve every file exactly once, and leave Inbox empty. Do not open the PDFs.

arc-cua requests are authored by Codex after observing the filenames. A request
provides its goal, literal inputs, constraints, verification criteria, and any
additional shortcuts needed. The helper accepts the standard public payload.
Include relevant app interaction hints in the subtask constraints or shortcut
descriptions. Prefer a bounded outcome that the returned UI can verify, such as
creating a folder or moving selected files to an existing destination. Codex inspects
each result and handles higher-level planning and recovery.

`verification` and `constraints` are arrays of non-empty strings, even when there
is only one entry. `inputs` maps names to literal strings/numbers/booleans;
every folder name or path JEV needs to type must be an input. `shortcuts` maps
uppercase chords such as `MOD+SHIFT+N` to descriptions; Return is the built-in
`PRESS_KEY` value `ENTER`, not an extra hotkey. `max_actions` is a positive integer.
The generated participant prompt contains a complete valid JSON example.
The helper validates the request before adding its workspace constraint or contacting JEV.

```sh
python examples/finder_preview/handoff.py --trial /path/to/demo/trials/arc-01 --app Finder --request /path/to/request.json
```

The helper activates the requested application without reopening an already-running
app, executes the subtask, and returns
the terminal result and final desktop snapshot. Traces record every returned
decision, executed action, and handoff duration. `decision_cycles` counts policy
decisions, not upstream Codex calls or internal provider retries.
The trace also retains the observations supplied to JEV and redacted error details.

## Completion and timing

The shared verifier checks exact paths and SHA-256 hashes. Missing documents,
wrong names or destinations, changed contents, extra files or folders, and symlinks fail the
trial. Finder's `.DS_Store` files are ignored. Finishing freezes either a success
or a failure; the agent cannot use a failed verification as an untimed answer key.

```sh
python examples/finder_preview/benchmark.py summary --root /path/to/demo --workflow filenames
```

The summary reports success counts and median successful completion times for
the selected workflow; filename and document-reading trials are never combined.
Run several trials, alternate their order, and retain failures. Mark trials with
human interruptions or tool approvals with `finish --invalid-reason "reason"`;
the summary excludes them from speed statistics. No measured results are included
in this repository yet.

## Recording

- Record each run from the same starting view at the same resolution.
- Composite them side by side with one elapsed timer per run.
- Use short captions at handoffs, such as "Create client folders" and "Move proposals".
- Keep playback speed identical on both sides and label any acceleration.
- Hold the completed side on the organized folders while the other finishes.
- End with verified completion times and the measured ratio.

Keep the app windows large. The visible transformation is a flat folder of
descriptive filenames becoming a complete client/document hierarchy.
