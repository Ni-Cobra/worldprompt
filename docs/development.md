# Development

How to run WorldPrompt from source, test it, build the release artifacts and cut a release. For how the pieces fit together, read [architecture.md](architecture.md) first.

## Prerequisites

| For | You need |
|---|---|
| Core | Python 3.11 or newer (CI and the packaged builds use 3.12). On Debian/Ubuntu install `python3-venv` |
| The mod | Java 25. The Gradle wrapper downloads everything else, including Minecraft, on first use |
| Running a real plan | The Codex CLI or Claude CLI, signed in. Tests and the packaged smoke test never need one |

## Run Core from source

```bash
python -m venv .venv
source .venv/bin/activate            # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e '.[test,build]'
worldprompt-core                     # settings GUI in your browser
```

The `test` extra adds pytest and the `build` extra adds PyInstaller. The Windows tray library (`pystray`) installs automatically on Windows only.

| Command or option | Effect |
|---|---|
| `worldprompt-core` | Same as `serve`: start Core and open the settings GUI |
| `worldprompt-core serve --no-browser` | Headless. Prints the private settings URL with its `#session=` fragment; treat it as a secret |
| `serve --no-tray` | Windows only: no notification-area icon (for services) |
| `serve --port <n>` | Settings GUI port for this launch |
| `--config <path>` | Settings file (before the subcommand: `worldprompt-core --config x.json serve`) |
| `--version`, `--help` | Version and usage |
| `WORLDPROMPT_CONFIG`, `WORLDPROMPT_CONNECTION_FILE` | Move the settings file and `connection.json`; see [paths and overrides](architecture.md#files-and-paths) |

`scripts/start.sh` runs `.venv/bin/python -m worldprompt_core` with the arguments you pass, from any directory. To develop without touching your real settings or a running Core, point both variables at a scratch directory and pick other ports:

```bash
export WORLDPROMPT_CONFIG=/tmp/wp-dev/config.json WORLDPROMPT_CONNECTION_FILE=/tmp/wp-dev/connection.json
worldprompt-core serve --no-browser --port 18765
```

## Tests

### Core

```bash
python -m pytest -q                          # everything
python -m pytest tests/test_world_jobs.py -q
python -m pytest tests/test_world_jobs.py -k rebuild -q
```

pytest puts `src/` on the path, so an editable install is optional. Most modules are plain pytest functions with fixtures; `test_agent.py` and `test_agent_integration.py` are `unittest.TestCase` classes. Platform-specific tests skip themselves (for example, `wslpath` translation on native Windows).

**Tests stay offline.** Nothing in the suite touches a model account. Provider CLIs are replaced by small Python script fixtures that print the same stdout, JSONL and exit codes the real Codex and Claude CLIs do, and the tests configure Core to run those scripts as the provider command. Discovery tests pin the CLI lookup so an installed, signed-in CLI on your machine cannot change the result.

Some tests pin contracts shared with the rest of the repository:

| Test | Pins | Regenerate |
|---|---|---|
| `test_import_graph.py` | Acyclic imports and the leaf-module rules | n/a |
| `test_version_sync.py` | Every version location agrees (see [Releases](#releases)) | n/a |
| `test_limits_sync.py` | The mod's `CoreLimits.java` equals the same-named constants in `limits.py` | n/a |
| `test_operations.py` with `tests/fixtures/plan_contract.json` | The generated plan schema, planner instructions and validation messages | `python -m tests.plan_contract` after an intentional contract change |
| `test_protocol_fixture.py` | The Core → mod result payload, shared with the mod's `ReplayPayloadTest` | `WORLDPROMPT_UPDATE_FIXTURES=1 python -m pytest tests/test_protocol_fixture.py` |

### Mod

```bash
cd fabric
./gradlew build                                      # JUnit + GameTests + jar
./gradlew test --tests dev.worldprompt.OperationCenterTest   # one JUnit class
```

`build` compiles the mod, runs the JUnit tests (`fabric/src/test/`), runs the GameTests (`fabric/src/gametest/`) and produces `fabric/build/libs/worldprompt-<version>.jar` plus a sources jar.

**GameTests** are Minecraft's in-game test framework: Loom starts a disposable dedicated server with the mod loaded, runs each test in a generated world and shuts down. That server lives under `fabric/build/run/gameTest/`, so tests never open or modify your installed Minecraft saves. The tests drive the real command and placement code against `FakeCore`, a scripted stand-in for Core, with `Chat` capturing player messages and `TickBudget` bounding waits in ticks. One GameTest compares the live game's block registry with Core's bundled schema (`src/worldprompt_core/schemas`), so it fails if the registry and the mod drift apart. JUnit's `ReplayPayloadTest` parses the golden `fabric/src/test/resources/complete-result.json`, the same file `test_protocol_fixture.py` checks from the Python side.

To try the mod in a real client, copy the jar from `fabric/build/libs/` into an instance's `mods` folder with Fabric API, and run Core from source; the mod finds it through `connection.json`.

## Build Core bundles

Core ships as a PyInstaller bundle per operating system. PyInstaller does not cross-compile: build on the OS you target.

```bash
python scripts/build_core.py
```

The script runs PyInstaller with `packaging/worldprompt-core.spec`, copies `LICENSE` into the bundle, prints build information (versions, platform, required glibc), runs the packaged smoke test and archives the result.

| Flag | Effect |
|---|---|
| `--skip-smoke` | Build without running the smoke test |
| `--bundle-root <dir>` | Stage the unpacked bundle there instead of `dist/`; archives still go to `dist/`. Use it to build while a Core is running from `dist/` |
| `--max-glibc <major.minor>` or `WORLDPROMPT_MAX_GLIBC` | Linux only: fail if any bundled ELF object needs a newer glibc |

| Output | Windows | Linux |
|---|---|---|
| Unpacked bundle | `dist/WorldPrompt-Core/` | `dist/linux/WorldPrompt-Core/` |
| Archive | `dist/WorldPrompt-Core-<version>-windows-<arch>.zip` | `dist/WorldPrompt-Core-<version>-linux-<arch>.tar.gz` |
| PyInstaller work files | `build/pyinstaller/windows` | `build/pyinstaller/linux` |

Archives hold the executable (`WorldPrompt-Core.exe` and `WorldPrompt-Core-Console.exe` on Windows, `WorldPrompt-Core` on Linux), the `_internal/` directory and `LICENSE`. The two Windows executables are described in [architecture.md](architecture.md#windows-and-wsl). `dist/` and `build/` are git-ignored local output.

**glibc floor.** A Linux bundle carries the build machine's Python and libraries, so it needs at least the glibc it was built against. Release archives are built in CI on Ubuntu 22.04 with `WORLDPROMPT_MAX_GLIBC=2.35`, which keeps them runnable on Ubuntu 22.04 and newer. A bundle built on a newer distribution can require a newer glibc; check the printed `required_glibc`.

### Packaged smoke test

`scripts/smoke_core.py <executable>` verifies a built bundle without any model account; `build_core.py` runs it unless you skip it. It launches the frozen executable against a temporary settings file and free ports, and a fake CLI script stands in for both providers. It checks:

- `--help` and `--version`, and on Windows that the two executables have the GUI and console subsystems.
- The settings GUI: the allowlisted static files, the settings page content and the authenticated state API. On Windows, a second launch exits and leaves the first running.
- Connection discovery: `connection.json` points at the mod API and an absolute token file, and the job list answers with the token.
- For Codex and Claude, each with fast mode on and off: saving settings, model discovery through the fake CLI, `/api/world/info`, and a real job through a packaged worker (submit, poll to `ready`, fetch the replay result and decode its placement runs, acknowledge application).
- The planner tool server: the fake CLI launches the exact MCP command the frozen worker emitted and calls block lookup, plan validation, region inspection and previews.
- Graceful shutdown through `/api/shutdown`: exit code 0 and `connection.json` removed.

### Wheel

```bash
python -m pip wheel . --no-deps -w dist
```

This produces `dist/worldprompt_core-<version>-py3-none-any.whl`. It includes the static GUI files and the bundled block registry as package data. The console entry point is `worldprompt-core`.

## Continuous integration

`.github/workflows/build.yml` runs on every push, pull request and manual dispatch, and is reused by releases.

| Job | Runs on | Steps |
|---|---|---|
| `core` | Ubuntu 22.04, Windows 2022 (Python 3.12) | `pip install ".[test,build]"`, `pytest -q`, `scripts/build_core.py` (with the glibc 2.35 limit on Linux), wheel (Linux only); uploads the archives and wheel |
| `fabric` | Ubuntu 22.04 (Temurin 25) | `./gradlew build`; uploads the mod jar |

A change that passes `pytest -q` and `./gradlew build` locally has passed the same checks CI runs.

## Releases

Versioning rules (shared MAJOR.MINOR, independent patches, one bump per branch, when to bump) are in the [README](../README.md#versioning). This section is the mechanics.

Version locations:

| Component | Locations |
|---|---|
| Core | `pyproject.toml`, `src/worldprompt_core/__init__.py`, the footer in `src/worldprompt_core/static/index.html` |
| Mod | `fabric/gradle.properties`, plus `fabric/README.md` (the `targets Minecraft` line, jar names and the `Use Core X with mod Y` sentence) |
| Both | The Download table links and the "Current versions" line in `README.md` |

`tests/test_version_sync.py` fails when these disagree.

To release, merge the version-bump commit into `master`, then push a tag for each component being released:

```bash
git tag core-vX.Y.Z   && git push origin core-vX.Y.Z      # Core
git tag fabric-vX.Y.Z && git push origin fabric-vX.Y.Z    # Fabric mod
```

Plain `vX.Y.Z` tags are ignored. `.github/workflows/release.yml` then:

1. Checks the tag against the source: `pyproject.toml` and `__init__.py` for `core-v*`, `fabric/gradle.properties` for `fabric-v*`. A mismatch fails the run.
2. Calls `build.yml` for that component only, which runs the full checks above.
3. Creates a draft GitHub release named for the component and version. It attaches `SHA256SUMS.txt` and the build outputs: the Windows zip, Linux tarball and wheel for Core, or the mod jar (not the sources jar) for the mod. Generated notes cover changes since the previous tag of the same component. Any unexpected file in the output aborts the release.

Review the draft on GitHub and publish it. The README download links already point at the new tags, since the bump commit updated them.

## Regenerating the block registry

`src/worldprompt_core/schemas/block-*-26.3.json` is generated data and is never edited by hand. Generate the official report with Java 25 (`java -DbundlerMainClass=net.minecraft.data.Main -jar server.jar --reports`), then run `scripts/import_block_report.py` with the report, Mojang's version metadata and the server jar. It verifies the version id and the server's SHA-1 before writing. What the registry holds and how the planner uses it is in [planner.md](planner.md).

## Adding a provider

A provider is a `Provider` subclass in `src/worldprompt_core/providers.py` that keeps every CLI-specific detail (command, input, event parsing, login check, failure classification, model picker) out of the generic driver in `agent.py`. The provider name is also listed in several other modules and in the GUI, so naming a new model is not enough. The checklist is in [planner.md](planner.md#adding-a-provider).

## Contributing

Issues and pull requests are welcome. Before opening one, run `python -m pytest -q`, and `./gradlew build` if you touched `fabric/`. Keep the docs in `docs/` in step with behavior changes; they are what the tests and the mod are written against. For anything large, talk to the maintainers first on [Discord](https://discord.gg/p5aHPnXAgB).
