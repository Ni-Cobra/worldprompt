# AGENTS.md

Guidance for coding agents (Claude Code, Codex and others) working in this repository.
It is the single source of agent instructions; `CLAUDE.md` only points here.

## The project

WorldPrompt lets Minecraft players build with their own ChatGPT (Codex CLI) or Claude
(Claude CLI) subscription. It has two independently versioned deliverables:

- **Core** (`src/worldprompt_core/`, Python): a local app with a browser settings page.
  It receives a captured selection from the mod, plans a build through the provider CLI,
  validates the plan and compiles an ordered replay. Core never touches the world.
- **The Fabric mod** (`fabric/`, Java, Minecraft 26.3): the `/wp` commands. It captures
  the selection, sends it to Core, places the replay over server ticks and keeps undo and
  rebuild history. The mod never talks to a model.

Read the docs relevant to your task before planning. They are the specification the
tests and the mod are written against, so keep them current when behavior changes.

| Doc | Covers |
|---|---|
| [`README.md`](README.md) | User guide, download links, versioning policy |
| [`docs/architecture.md`](docs/architecture.md) | Components, job lifecycle, process isolation, security, paths, module layering |
| [`docs/minecraft.md`](docs/minecraft.md) | `/wp` commands, permissions, lecterns, mod config |
| [`docs/protocol.md`](docs/protocol.md) | Core ↔ mod HTTP protocol and replay payload |
| [`docs/planner.md`](docs/planner.md) | Providers, planning passes, plan operations, planner tools, limits, block registry |
| [`docs/vps.md`](docs/vps.md) | Headless Linux server install |
| [`docs/development.md`](docs/development.md) | Building, testing, CI and releases |

## Commands

Python uses the repository virtual environment `.venv` (Python 3.12). This checkout is a
WSL working copy that also has a native Windows interpreter at
`work/venv-win/Scripts/python.exe`; use it to run the Windows half of the checks
without leaving WSL. Other checkouts may not have it.

```bash
.venv/bin/python -m pytest -q                          # full Core suite
.venv/bin/python -m pytest tests/test_core.py -q       # one file
.venv/bin/python -m pytest tests/test_world_jobs.py -k rebuild -q
work/venv-win/Scripts/python.exe -m pytest -q          # same suite, native Windows

.venv/bin/python scripts/build_core.py                 # Linux bundle + packaged smoke test
work/venv-win/Scripts/python.exe scripts/build_core.py # Windows bundle + packaged smoke test
scripts/start.sh serve --no-browser                    # run Core from source

cd fabric && ./gradlew build                           # Java 25: JUnit, isolated GameTests, jar
cd fabric && ./gradlew test --tests dev.worldprompt.OperationCenterTest
```

`build_core.py` flags: `--skip-smoke`, `--bundle-root <dir>` (build elsewhere, e.g. while
Core runs from `dist/`), `--max-glibc` / `WORLDPROMPT_MAX_GLIBC`. The packaged smoke test
never uses a model account. Nothing in the test suites calls a provider: CLI providers are
replaced with script fixtures that emit the same output shapes. Most tests are plain
pytest; `test_agent.py` and `test_agent_integration.py` are `unittest.TestCase` classes.
GameTest worlds stay under `fabric/build/` and never touch real Minecraft saves.

## Invariants

Details are in the docs above; these are the rules that are easy to break.

- **The plan is the contract.** `operations.py` holds one spec per operation and generates
  the JSON schema, shape checks, field checks and the planner's instruction text. Adding an
  operation is one spec there plus its geometry in `core.apply_plan`. A new operation or
  any change to the replay payload is a Core ↔ mod API change.
- **Replay is ordered, not a diff.** `replay.compile_replay` applies the plan once and
  records every operation's placement runs, so the mod reproduces authoring order;
  `/wp rebuild` replays it with fresh random block choices and no model call.
- **Volume layout.** `BlockVolume` (`core.py`) is a palette plus a NumPy array shaped
  `(height, length, width)`; public coordinates are `[x, y, z]`, flattened x-fastest, then
  z, then y. Plan coordinates are local, inclusive and never expand the selection.
- **Every limit lives in `limits.py`**, and model-facing text is built from it. Don't
  hard-code a budget or limit anywhere else. The mod mirrors the ones it checks in
  `CoreLimits.java`, kept equal by `tests/test_limits_sync.py`.
- **Block registry** files under `src/worldprompt_core/schemas/` are generated. Regenerate
  them only with `scripts/import_block_report.py`.
- **Acyclic imports**, function-local imports included, enforced by
  `tests/test_import_graph.py`. No module imports another module's `_private` names.
- **Providers.** Everything CLI-specific lives in a `Provider` subclass in
  `providers.py`; `agent.py` is the generic driver. Adding a provider also means listing
  its name everywhere the existing two are listed (see `docs/planner.md`).
- **Security is part of the design.** Everything binds to loopback; the settings page
  uses a per-launch token plus Host/Origin checks and a CSP; the mod uses a separate
  adapter token file; job artifacts are created 0600/0700. Core never reads or relays
  provider credentials. Don't weaken any of this as a side effect.
- **Mod threading.** Capture and placement run on the server tick thread, bounded by
  `blocksPerTick`; networking runs off-thread.

## Working rules

- Inspect the working tree before editing and preserve unrelated user changes.
- Keep changes scoped to the request. Update the affected docs in the same change.
- Do not maintain `dist/`. It is git-ignored local build output; builds may write there,
  but don't refresh it as a chore, never commit it, and don't delete old archives in it
  unless asked.

## Versioning

The policy is in [`README.md#versioning`](README.md#versioning). In short, for agents:

- **Do not bump versions while working.** Normal changes, fixes and follow-ups leave every
  version string alone.
- **Bump once per branch, only when the user says the branch is ready to merge into
  `master`** (or explicitly asks for the release bump). The bump is the last commit on the
  branch.
- At that point, compare the branch with `master` (`git diff master...HEAD`) and bump each
  component whose shipped code, resources or packaging changed:
  - patch bump for that component, or
  - the next minor for **both** components if the Core ↔ mod API changed (protocol,
    payload, operations), with patch reset to 0;
  - major only when the user says so.
  Changes limited to docs, README, CI or agent guidance don't bump anything. An untouched
  component keeps its version even if the other's patch moves ahead.
- Version locations, all checked by `tests/test_version_sync.py`:
  - Core: `pyproject.toml`, `src/worldprompt_core/__init__.py`, and the footer version in
    `src/worldprompt_core/static/index.html`.
  - Mod: `fabric/gradle.properties`.
  - `README.md`: the Download table links and the `Current versions:` line.
  - `fabric/README.md`: the `WorldPrompt X.Y.Z targets Minecraft` line, jar names and
    the `Use Core X.Y.Z with mod X.Y.Z` sentence.
  Then search the repository for the old version string to catch anything else.
- Releases are made from `master` by pushing `core-vX.Y.Z` / `fabric-vX.Y.Z` tags. Only push
  tags when the user asks.

## Verification

- Run the checks for every component you touched:
  - Core: the relevant pytest files while iterating, the full suite before finishing.
    Also run it with the Windows interpreter when it is available and the change could
    behave differently on Windows (processes, paths, tray, CLI discovery).
  - Fabric: `./gradlew build` in `fabric/`.
  - Packaging, launcher, tray or anything that behaves differently when frozen:
    `scripts/build_core.py` on each available native platform, with the smoke test on.
- For the merge bump, run the full Core suite (Linux, and Windows when available) and
  `./gradlew build`, whichever components changed.
- Report what ran and what didn't, with real results, in your final message or PR
  description. Never claim a check you didn't run. Don't write verification logs into
  the docs; CI on the pushed branch is the lasting record.

## Git

- Run `git diff --check` and review the full diff before finishing.
- Commit only when the user asks; the merge bump is committed as part of that request.
  Push only when the user explicitly asks.
