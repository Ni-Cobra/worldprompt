# Development

How to run WorldPrompt from source, test it, build the release artifacts and cut a release. For how the pieces fit together, read [architecture.md](architecture.md) first.

## Prerequisites

| For | You need |
|---|---|
| Core | Python 3.11 or newer (CI and the packaged builds use 3.12). On Debian/Ubuntu install `python3-venv` |
| The mod | Java 25, Git and Python 3.11 or newer. Bootstrap Mycelium before building. NeoForge uses Python to adapt GameTests. The Gradle wrapper downloads Gradle and Minecraft dependencies on first use. |
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
./mycelium prepare                  # pinned compiler source
./gradlew build                                      # both loaders, JUnit + GameTests + jars
./gradlew :fabric:test --tests dev.worldprompt.OperationCenterTest
./gradlew :neoforge:build                             # one loader
```

Use Java 25. Both loader modules compile the shared `common/` and `pure/` sources with
Mycelium. The [Mycelium guide](mycelium.md) explains the source layout, compiler pin and
workflow. Root `build` produces `fabric/build/libs/worldprompt-fabric-<version>.jar` and
`neoforge/build/libs/worldprompt-neoforge-<version>.jar`, along with sources jars. Each
loader also has a wrapper for focused development.

Both loaders run the same JUnit sources (`fabric/src/test/`) and behavioral GameTests
(`fabric/src/gametest/`). NeoForge's `generateGameTests` adapts the test registrations and
native permission fixture to its framework; it preserves the shared test bodies and rejects
unconverted Fabric dependencies. Generated test code stays under `neoforge/build/` and
never enters a release jar.

Catalog tests register a test-only block through each loader's native registry setup. They
check property domains and defaults, HTTP submission, modded-block placement and undo. A
separate no-op test environment isolates the asynchronous catalog round trip from the
distance and sound scenario. The NeoForge adapter checks the environment resource before
translating the registration, so changes to that resource need a matching native mapping.

**GameTests** use Minecraft's in-game test framework. Fabric Loom and NeoForge ModDev each
start a disposable dedicated server, run the tests in a generated world and shut down. The
servers live under their loader's `build/run/gameTest/` directory and do not touch installed
Minecraft saves. Tests exercise the command and placement code against `FakeCore`, a scripted
stand-in for Core. `Chat` captures player messages, and `TickBudget` bounds waits in ticks.
One test compares the live block registry with Core's bundled schema
(`src/worldprompt_core/schemas`). JUnit's `ReplayPayloadTest` parses
`fabric/src/test/resources/complete-result.json`, also checked by Python's
`test_protocol_fixture.py`.

For ordinary client development, run either loader directly:

```bash
./gradlew :fabric:runClient
./gradlew :neoforge:runClient
```

These runs load WorldPrompt's main sources. Fabric keeps its game files in `fabric/run/client/`; NeoForge uses
`neoforge/runs/client/`. Both directories are ignored by Git. The lane hook and GameTests belong to separate test runs.

To check a release jar, put it in a fresh Minecraft instance for that loader. Fabric also needs Fabric API. Start Core
under the same OS user; the mod finds it through `connection.json`. A single-player world with commands enabled can
exercise selection, planning, placement, undo and rebuild through the normal chat commands.

For the real Fabric client/server path, use the [runtime lane](../tools/lane/README.md):
build Fabric, then run `python tools/lane/lane.py gen` to generate its run configurations.
Run `python tools/lane/lane.py up`, followed by `python tools/lane/lane.py smoke`,
`python tools/lane/lane.py edges` and `python tools/lane/lane.py down`. Its local Core
fixture supplies repeatable plans without a model account. Inspect the lane's assertions,
client observations and screenshots when a scenario fails. The lane uses a separate
temporary game world and does not prove a different loader or Minecraft version.

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
| `mod` (Fabric and NeoForge matrix) | Ubuntu 22.04 (Temurin 25, Python 3.12) | Bootstrap the pinned compiler; `./gradlew :<loader>:build`; upload that loader's mod jar (`worldprompt-<loader>-<version>.jar`) |

Run `pytest -q` and `./gradlew build` locally before submitting. CI also builds and smoke-tests native Core bundles on Linux and Windows; those packaging checks need their respective operating systems.

## Releases

Versioning rules (shared MAJOR.MINOR, independent patches, one bump per branch, when to bump) are in the [README](../README.md#versioning). This section is the mechanics.

Version locations:

| Component | Locations |
|---|---|
| Core | `pyproject.toml`, `src/worldprompt_core/__init__.py`, the footer in `src/worldprompt_core/static/index.html` |
| Mod (both loaders) | `fabric/gradle.properties`, plus `fabric/README.md` (the `targets Minecraft` line, jar names and the `Use Core X with mod Y` sentence) and `neoforge/README.md` (jar name and the Core version) |
| Both | The Download table links and the "Current versions" line in `README.md`, the fake Core version in `tools/lane/lane.py`, and the jar name in `tools/lane/README.md` |

Fabric and NeoForge are one component. They read `fabric/gradle.properties`, so they always carry the same version and are released together; see the [README](../README.md#versioning).

`tests/test_version_sync.py` fails when these disagree.

To release, merge the version-bump commit into `master`, then push a tag for each component being released. The mod's two loader tags are pushed as a pair, at the same version and on the same commit:

```bash
git tag core-vX.Y.Z   && git push origin core-vX.Y.Z      # Core
git tag fabric-vX.Y.Z && git push origin fabric-vX.Y.Z    # Fabric mod
git tag neoforge-vX.Y.Z && git push origin neoforge-vX.Y.Z # NeoForge mod
```

Plain `vX.Y.Z` tags are ignored. `.github/workflows/release.yml` then:

1. Checks the tag against the source: `pyproject.toml` and `__init__.py` for `core-v*`, `fabric/gradle.properties` for both loader tags. A mismatch fails the run. For a loader tag it also holds the shared mod component: if the other loader's tag exists it must name the same version and the same commit, and this commit must not already be tagged for that loader at a different version. It cannot require the sibling tag itself, since whichever is pushed first arrives alone; pushing both is the policy, not a gate.
2. Calls `build.yml` for that component only, which runs the full checks above.
3. Creates a draft GitHub release named for the component and version. It attaches `SHA256SUMS.txt` and the build outputs: the Windows zip, Linux tarball and wheel for Core, or the mod jar (not the sources jar) for the mod. Generated notes cover changes since the previous tag of the same component. Any unexpected file in the output aborts the release.

Review the draft on GitHub and publish it. The README download links already point at the new tags, since the bump commit updated them.

## Regenerating the block registry

`src/worldprompt_core/schemas/block-*-26.3.json` is generated data and is never edited by hand. Generate the official report with Java 25 (`java -DbundlerMainClass=net.minecraft.data.Main -jar server.jar --reports`), then run `scripts/import_block_report.py` with the report, Mojang's version metadata and the server jar. It verifies the version id and the server's SHA-1 before writing. What the registry holds and how the planner uses it is in [planner.md](planner.md).

## Adding a provider

A provider is a `Provider` subclass in `src/worldprompt_core/providers.py` that keeps every CLI-specific detail (command, input, event parsing, login check, failure classification, model picker) out of the generic driver in `agent.py`. The provider name is also listed in several other modules and in the GUI, so naming a new model is not enough. The checklist is in [planner.md](planner.md#adding-a-provider).

## Contributing

Issues and pull requests are welcome. Before opening one, run `python -m pytest -q`, and `./gradlew build` if you touched the Java mod or its build. Keep the docs in `docs/` in step with behavior changes; they are what the tests and the mod are written against. For anything large, talk to the maintainers first on [Discord](https://discord.gg/p5aHPnXAgB).
