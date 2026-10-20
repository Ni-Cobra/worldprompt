<p align="center">
  <img src="logo-circle.svg" alt="WorldPrompt logo: a quill writing beside three outlined blocks" width="160">
</p>

<h1 align="center">WorldPrompt</h1>

<p align="center">
  <b>It's like WorldEdit... but AI</b><br>
  A Minecraft 26.3 mod for Fabric and NeoForge that builds with your ChatGPT (Codex) or Claude subscription.<br>
  Works in single-player and on multiplayer servers. Players don't need to install anything.
</p>

<p align="center">
  <a href="https://discord.gg/p5aHPnXAgB"><img alt="Join the Discord" src="https://img.shields.io/badge/Discord-Join%20the%20server-5865F2?style=for-the-badge&logo=discord&logoColor=white"></a>
  <a href="https://maniacobra.com"><img alt="Website: maniacobra.com" src="https://img.shields.io/badge/Website-maniacobra.com-f4a261?style=for-the-badge"></a>
</p>

---

A tool for Minecraft builders to invoke AI agents who are following your building styles and patterns, letting you automate in the background whatever part of your project you want.
Use it to fill your empty buildings with details or spawn large-scale structures to decorate whole selected regions.
The agents will follow your specs surgically, use lecterns to add more context. Make sure to prompt well and to use the right AI models!

---

## ⬇️ Download

<a id="download"></a>

WorldPrompt has two parts, the **mod** and the **core**. Downloads :

| Part | File |
|---|---|
| **The mod** for **Fabric**, for your `mods` folder | [`worldprompt-fabric-1.0.0.jar`](https://github.com/Ni-Cobra/worldprompt/releases/download/fabric-v1.0.0/worldprompt-fabric-1.0.0.jar) |
| **The mod** for **NeoForge**, for your `mods` folder | [`worldprompt-neoforge-1.0.0.jar`](https://github.com/Ni-Cobra/worldprompt/releases/download/neoforge-v1.0.0/worldprompt-neoforge-1.0.0.jar) |
| **WorldPrompt Core** for Windows: the local app that talks to the AI | [`WorldPrompt-Core-1.0.0-windows-x86_64.zip`](https://github.com/Ni-Cobra/worldprompt/releases/download/core-v1.0.0/WorldPrompt-Core-1.0.0-windows-x86_64.zip) |
| **WorldPrompt Core** for Linux | [`WorldPrompt-Core-1.0.0-linux-x86_64.tar.gz`](https://github.com/Ni-Cobra/worldprompt/releases/download/core-v1.0.0/WorldPrompt-Core-1.0.0-linux-x86_64.tar.gz) |

Current versions: **mod 1.0.0** and **Core 1.0.0**, for **Minecraft 26.3**. Patch
numbers can differ between the two. Older versions are on the
[releases page](https://github.com/Ni-Cobra/worldprompt/releases).

**Requirements:**

- Your loader for Minecraft 26.3, and nothing from the other one:
  - **Fabric:** [Fabric Loader](https://fabricmc.net/use/installer/) **and**
    [Fabric API](https://modrinth.com/mod/fabric-api)
  - **NeoForge:** [NeoForge](https://neoforged.net/) 26.3.0.92 or newer. Fabric API is
    neither needed nor usable here.
- A ChatGPT or Claude subscription with its CLI: [Codex CLI](https://developers.openai.com/codex/cli)
  or [Claude Code](https://code.claude.com/docs/en/setup) (2.1.248 or newer). Core
  never installs or updates the CLI; if a planning run reports an incompatible CLI,
  update it.

No API key needed: WorldPrompt runs on the subscription you already have, and your
credentials stay with the CLI.

Install the jar for your loader and only that one. The two jars are the same mod and the
same version: the same `/wp` commands, configuration and Core connection. Mycelium is used
during development and is not an additional mod to install. See
[developing with Mycelium](docs/mycelium.md).

## 🚀 Setup

1. **Install the mod.** Remove any older WorldPrompt jar, then put the jar for your
   loader in your `mods` folder:
   - **Fabric:** `worldprompt-fabric-<version>.jar`, together with Fabric API.
   - **NeoForge:** `worldprompt-neoforge-<version>.jar`. Nothing else is needed.
2. **Sign in to the CLI.** Run `codex login` or `claude auth login` once in a terminal,
   using the same computer and user account you play Minecraft on.
3. **Start Core.** Extract the whole archive, then run `WorldPrompt-Core.exe` on Windows
   or `./WorldPrompt-Core` on Linux. Keep the `_internal` folder next to the executable.
4. **Connect.** In the settings page Core opens in your browser, pick Codex or Claude,
   click **Check connection**, then **Save settings**.
5. **Play.** Leave Core running while you play. Closing the browser tab doesn't stop
   it. On Linux, stop it with Ctrl+C in its terminal.

> [!IMPORTANT]
> **Windows will warn you about `WorldPrompt-Core.exe`. That's expected.** The app
> isn't code-signed, so SmartScreen shows "Windows protected your PC" and some
> antivirus software asks about it. Click **More info → Run anyway**, and **Allow** it
> in your antivirus if prompted. Releases are built from this repository by GitHub
> Actions, and `SHA256SUMS.txt` lets you check your download.

On Windows, Core runs in the notification area (it may be under hidden icons). Its
menu has **Open settings** and **Exit Core**, and launching the exe again also
reopens the settings. Core binaries are x86_64 only, and Linux builds need glibc 2.35
or newer (Ubuntu 22.04+).

### First build

Open the game, make sure you have operator privileges (cheats on, in single-player).
Look at the ground and type:

```text
/wp select
/wp prompt Build a cozy woodland cabin with a stone chimney
```

Then just wait for it to get built.

Use `/wp pos1` and `/wp pos2` for more control over the selection.

## 🩺 Troubleshooting

| Problem | Fix |
|---|---|
| The mod can't reach Core | Core must run on the same machine and user account as Minecraft. Start order doesn't matter. |
| **Check connection** fails | Sign in again with the CLI, then restart Core. After installing a CLI, restart Core so it can find it. |
| No permission for `/wp prompt` | You need operator status (cheats on, in single-player). See [permissions](#who-can-use-what). |
| The build is cut off | The AI only builds inside your selection, so select more space, especially above the ground. |
| Multiplayer server | WorldPrompt is fully multiplayer-compatible: only the server needs the mod, players join with a vanilla client, and Core runs on the server machine. See the [VPS guide](docs/vps.md). |

Still stuck? Ask on **[Discord](https://discord.gg/p5aHPnXAgB)**.

**Privacy and usage:** your prompt, prompt books, a description of the selected blocks,
your position and view direction, and preview images go to the provider you chose,
through its CLI, and count against your subscription. Double pass uses about twice as
much. Job data stays in a private folder on your machine until you remove it from
**Recent jobs** in the settings page.

---

## 🎮 Command guide

All commands start with `/wp` (alias `/worldprompt`). The flow is: **select → prompt → keep, undo or rebuild.**

### 1. Select an area

| Command | What it does |
|---|---|
| `/wp select` | Selects a 30 × 30 × 30 cube around the block you're looking at, at any distance within loaded chunks. |
| `/wp select 50 20 30` | Same, with custom X, Y and Z sizes. |
| `/wp pos1 [x y z]` / `/wp pos2 [x y z]` | Sets the two corners, at your position by default. |

Selections are outlined with particles. The AI never builds outside the selection,
so leave room above the ground. A selection is limited to 2,000,000 blocks by default
(server setting `maxSelectionBlocks`, 4,000,000 at most).

### 2. Describe your build

| Command | What it does |
|---|---|
| `/wp prompt <description>` | Plans and builds in a single pass (the default). `/wp prompt single <description>` is the same. |
| `/wp prompt double <description>` | Drafts, renders a preview, reviews it, then builds the revised plan. Slower and uses about twice the usage, but usually better. |

The AI sees the existing blocks, so you can edit builds as well as create them:
"turn this hill into a terraced vineyard", "add a second floor to this house".

### 3. Manage jobs

| Command | What it does |
|---|---|
| `/wp jobs [page]` | Lists running jobs with their short IDs (like `a1b2c3d4`): yours first, then other players'. |
| `/wp cancel [id]` | Cancels a job (your newest by default). Cancelling a rebuild also restores the original blocks, which needs the `undo` permission. |
| `/wp undo [id]` | Restores the blocks from before an edit. Repeat to walk back through up to 16 edits. |
| `/wp rebuild [id]` | Replays a saved plan with fresh random block choices. No AI call, and Core can be offline. |
| `/wp book [id]` | Gives another copy of a build's summary book. |
| `/wp delay <ticks>` | Pauses between operations while building (20 ticks = 1 s, up to 72,000). The default is `1`; `0` places everything in a single tick. Applies to your future prompts and rebuilds. |
| `/wp info` | Shows Core's version, model settings, job counts, timeouts and selection limits. |

Several jobs can run at once if their selections don't overlap. Undo and rebuild
history lasts until the server stops, and the undo depth (16) is the `maxUndoEdits`
server setting.

### Room-by-room instructions with lecterns

Put a lectern with a **prompt book** in any area of the selection to give that area its
own instructions. A prompt book is a book titled or anvil-renamed `prompt`, or one
whose text starts with `prompt`, for example `Prompt: kitchen with stone counters`.
Then run `/wp prompt` with the overall request as usual. The lecterns stay in place
unless you say otherwise. Limits: 64 lecterns, 4,000 characters per book, 16,000 in total.

### Who can use what

- **Everyone:** `pos1`, `pos2`, `select`, `jobs`, `cancel`, `book`, `delay`, `info`.
- **Operators only (by default):** `prompt`, `undo`, `rebuild`.

Permission mods such as [LuckPerms](https://luckperms.net/) can override each command
through `worldprompt.command.<name>`, e.g.
`/lp group builder permission set worldprompt.command.prompt true`. Every prompt uses
the Core operator's subscription, so grant `prompt` carefully. Details: [permissions](docs/minecraft.md#permissions).

Full command reference and server settings: [docs/minecraft.md](docs/minecraft.md).

---

## 🛠️ For developers and contributors

### How it works

```
browser settings page ──http 127.0.0.1:8765──▶ WorldPrompt Core (Python)
Minecraft mod ──http 127.0.0.1:8768 + adapter token──▶ Core job supervisor
                                                     └▶ worker process per job
                                                         └▶ Codex CLI | Claude CLI
                                                         └▶ validate → replay
Minecraft mod ◀── downloads the validated replay and places it over server ticks
```

- **Core** (`src/worldprompt_core/`) never touches the world. It receives the
  captured selection, plans through the selected CLI, validates the plan against the
  server's live block catalog, and compiles an ordered replay. For requests without a
  live catalog, Core uses the bundled vanilla catalog.
- **The mod** (`common/`, `pure/`, `fabric/`, `neoforge/`) never talks to a model. It captures the selection,
  re-validates block states against the live game registry, places the replay, and
  keeps the undo snapshot and the plan for `/wp rebuild`.
- Everything binds to loopback only. The settings page uses a per-launch token and
  strict Host/Origin checks, the mod uses a separate adapter token file, and Core
  never reads or relays provider credentials.

| Topic | Document |
|---|---|
| Components, job lifecycle, process isolation, security, paths | [docs/architecture.md](docs/architecture.md) |
| `/wp` commands, permissions, lecterns, mod configuration | [docs/minecraft.md](docs/minecraft.md) |
| Core ↔ mod HTTP protocol and replay payload | [docs/protocol.md](docs/protocol.md) |
| Providers, planning passes, plan operations, planner tools, limits, block registry | [docs/planner.md](docs/planner.md) |
| Headless/VPS install with SSH and systemd | [docs/vps.md](docs/vps.md) |
| Building, testing, CI and releases | [docs/development.md](docs/development.md) |
| Shared development, loader boundaries and Mycelium | [docs/mycelium.md](docs/mycelium.md) |
| The mod's install notes and compatibility | [fabric/README.md](fabric/README.md) |

### Build from source

Core needs Python 3.11+ (3.12 is tested) and the mod needs Java 25. A quick start from
a checkout:

```bash
python -m venv .venv && source .venv/bin/activate   # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e '.[test,build]'
worldprompt-core                                     # or: worldprompt-core serve --no-browser
python -m pytest -q                                  # Core tests (never call a real AI account)
./mycelium prepare                 # pinned build-time compiler
./gradlew build                                      # both mod jars, JUnit and GameTests
```

Running headless, building the Windows and Linux bundles, CI and releases are covered in
[docs/development.md](docs/development.md).

### Versioning

There are two versioned components: **Core** and **the mod**. Fabric and NeoForge are
not separate components. They are one mod built for two loaders: they read their version
from the same `fabric/gradle.properties`, so they always carry the same version, and they
are released together from the same commit, as a matching pair of `fabric-vX.Y.Z` and
`neoforge-vX.Y.Z` tags. Their jars differ only in loader and name
(`worldprompt-fabric-<version>.jar` and `worldprompt-neoforge-<version>.jar`). Never
release or bump one loader without the other.

Core and the mod share `MAJOR.MINOR`, and `MINOR` is the Core ↔ mod API: a change that
breaks compatibility between them raises it in both. `PATCH` is independent per
component. `MAJOR` changes only when the maintainer decides. Core and the mod check
this themselves: a pair whose `MAJOR.MINOR` differs refuses to run jobs and says which
version to install.

Versions are not bumped per change or per commit. A branch gets one version bump, made
as its last commit when it is ready to merge into `master`: each component whose shipped
code, resources or packaging changed gets a patch bump, or both components get the next
minor version if the Core ↔ mod API changed. A component the branch didn't touch keeps
its version. Changes to docs, the README, CI or agent guidance alone bump nothing.

Releases are cut from `master` by pushing a `core-vX.Y.Z` tag, or the mod's
`fabric-vX.Y.Z` and `neoforge-vX.Y.Z` pair. CI builds and verifies each tagged jar or
bundle and drafts its GitHub release. The release workflow rejects a loader tag that
contradicts its sibling — one on a different commit, or this commit already tagged for
the other loader at a different version — but it cannot require a sibling that has not
been pushed yet, so pushing the pair together stays a matter of following the policy (see
[docs/development.md](docs/development.md)). Each bump updates every place the version
lives, including the download links and `Current versions:` line in this README, and
`tests/test_version_sync.py` checks that they agree. The full agent workflow is in
[AGENTS.md](AGENTS.md).

### Settings and data

| Item | Windows | Linux |
|---|---|---|
| Settings | `%LOCALAPPDATA%\WorldPrompt\config.json` | `${XDG_CONFIG_HOME:-~/.config}/worldprompt/config.json` |
| Data, `connection.json` | `%LOCALAPPDATA%\WorldPrompt` | `${XDG_DATA_HOME:-~/.local/share}/worldprompt` |
| Job artifacts and the adapter token | `<data>\jobs\world` | `<data>/jobs/world` |
| Mod settings | `config/worldprompt.json` in the Minecraft folder | same |

- **Overrides:** `WORLDPROMPT_CONFIG` or `--config <path>` moves the settings file, and
  `WORLDPROMPT_CONNECTION_FILE` moves `connection.json`. The job folder and port are
  settings in the page.
- **CLI discovery:** when a provider's command field is empty, Core searches PATH, then
  the native user install (`~/.local/bin`), desktop app CLI caches (including the Windows
  Store Claude app) and VS Code extensions. A filled field is used exactly as entered,
  such as `["C:\\Users\\you\\.local\\bin\\claude.exe"]`.
- **Models:** the dropdown lists what each CLI reports. The default, **CLI default**,
  lets each CLI use its own default model. Each provider has its own reasoning-effort
  (default `medium`) and fast-mode (default off) settings.
- **Limits:** at most 4 million blocks per selection in Core (the mod defaults to 2
  million), up to 512 operations per pass, 80 million geometry work units and 64 MiB
  of placement data per plan. Job limits are off by default: no pass timeout, no cap
  on planners running at once, queue size or jobs per player. Turn on **Apply job
  limits** in the settings page to set them (timeouts per pass, operations per plan,
  concurrency, queue capacity and jobs per player). They aren't spending caps. See
  [planner limits](docs/planner.md#limits).
- **Mod connection:** the mod discovers Core through `connection.json`, which it
  re-reads on every request. Explicit `serviceUrl`/`tokenFile` settings in
  `config/worldprompt.json` take precedence. Core and the Minecraft server must run
  on the same machine.

### Contributing

Issues and pull requests are welcome. Come say hi on
[Discord](https://discord.gg/p5aHPnXAgB) before starting something big.

Adding another AI provider means adding a `Provider` in `src/worldprompt_core/providers.py`
and registering it; naming a model isn't enough. The checklist is in
[docs/planner.md](docs/planner.md#adding-a-provider).
