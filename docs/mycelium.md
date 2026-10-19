# Developing with Mycelium

WorldPrompt targets Fabric and NeoForge. Mycelium helps keep behavior shared while expressing platform differences at
explicit, checked seams. It is a build tool and a set of source packages, not another mod loader or a runtime library
that server owners must install.

WorldPrompt can use the full Fabric or NeoForge API. At build time, Mycelium copies selected source into WorldPrompt's
namespace, and those sources compile into its jar. Most WorldPrompt work stays in this repository; use a reusable
contract where its guarantees help the feature.

## Where a change belongs

| Change | Start here |
|---|---|
| Planning, validation, Core settings or the browser UI | `src/worldprompt_core/` and `tests/` |
| Pure Java rules that use only the JDK | `pure/src/main/java/` |
| Shared game behavior, the command host, protocol or loader-neutral game code | `common/src/main/java/` |
| Fabric events, setup or APIs | `fabric/src/main/java/` |
| NeoForge events, setup or APIs | `neoforge/src/main/java/` |

The Fabric and NeoForge modules compile the same `common/` and `pure/` sources. Put a normal feature change there once,
then keep loader-specific registration and API calls in that loader's module. `pure/` compiles without Minecraft or a
loader. Minecraft types belong in `common/` or a native module.

## What changed in this port

The command host, Core client, selection, undo and rebuild history, and placement implementation moved from the Fabric
module into shared sources. Fabric and NeoForge provide their own entrypoints, events and permission bindings, then
compile the same feature code. The command names, configuration location, Core connection discovery and replay payload remain the same
across loaders. Servers do not need to install a Mycelium mod. Each live job can include the server's block catalog;
Core keeps its bundled vanilla catalog for offline work and requests from older mods that omit the snapshot.

The permission decision and resumable replay cursor use checked Mycelium contracts. Java 25 compiler support was
developed in Mycelium alongside them. Both loader builds run the unit tests and in-game scenarios. Packaged-server lanes
exercise player commands, world changes, undo, rebuild and failure paths on both loaders. These lanes use a Fabric test
client without WorldPrompt, including when connecting to the NeoForge server. On Fabric, existing player and server
behavior, configuration and runtime dependencies are preserved.

## When Mycelium helps

Use it when a behavior has a real contract across versions or loaders: a permission result whose explicit denial must
override an operator default, a state transition with rules both platforms must preserve, or a native API that has
meaningfully different realizations. A contract gives the behavior a typed Java model and laws; a bridge supplies its
Minecraft implementation, with loader overlays only where a loader API is involved.

For a rename, a one-off registration detail or ordinary shared behavior, use the Java and loader modules directly.
Keep WorldPrompt-specific adapters and feature decisions here. Add a Mycelium contract when it makes an important
guarantee checkable; a feature does not need one by default.

Command permissions are one example. The mod asks the `command.authorization@1` model whether a platform's grant,
denial or unset result authorizes a command. The `command-authorization` bridge adapts the native command source and
provides Fabric and NeoForge implementations. Before changing this behavior, read the contract's `SPEC.md` and the
bridge's `bridge.json` in the Mycelium checkout selected by the build. Pass the command's effective fallback privilege
through the call so native APIs preserve source-specific permissions.

Ordered placement uses `world.replay@1`. As the HTTP payload is parsed off-thread, its immutable program validates and
copies the run data. When an operation starts, the placement port creates a fresh cursor that shares the program's
immutable data and tracks its own work budget and resume position. WorldPrompt's port handles game operations such as
block choices, warnings, matching, sounds and undo. The contract defines traversal behavior and its rejection matrix.

When an important feature guarantee needs to change, develop its contract and use together. Reusable compiler and
compatibility improvements belong in Mycelium.

Block discovery uses `block.catalog@1` and its `block-catalog` bridge. The bridge reads the initialized native registry,
including mod namespaces, into an immutable model of property domains and defaults. WorldPrompt applies protocol limits
and sends the snapshot with each job. Core uses it for planner tools, previews, validation and replay compilation. It
does not mix catalogs from different servers or silently add blocks from the vanilla fallback.

## Choosing the project shape

| Approach | What it means here |
|---|---|
| Fabric only | One loader, one native entrypoint and one artifact. Shared contracts can still cover behavior across Minecraft versions. |
| Separate Fabric and NeoForge implementations | This is possible, but repeated feature logic tends to drift. WorldPrompt shares its game host and feature behavior in `common/` and splits only loader-specific bindings. |
| Architectury or another broad platform framework | This can be a good fit when a project wants a general cross-loader API and shared abstractions across most game code. WorldPrompt uses Mycelium for narrower, typed compatibility contracts and keeps direct loader APIs available. |
| Mycelium contract and bridge | Use this for a behavior whose meaning or guarantee must stay consistent across targets, with a native realization where needed. It does not replace the rest of the mod's Java structure. |

Choose the structure that makes behavior easy to understand and verify for WorldPrompt’s supported targets.

## Building the mod

The repository root wrapper builds and checks both loader modules. `check` runs both modules' checks; `build` also
assembles both jars. Each loader has a wrapper for focused work. Minecraft 26.3 mod sources require Java 25. Mycelium's
Gradle plugin and source layers have different Java requirements, which the build handles separately.

```powershell
# From the WorldPrompt root, build and check Fabric and NeoForge.
.\gradlew check

# To focus on one loader instead (use neoforge/ for NeoForge):
Push-Location fabric
try { .\gradlew build } finally { Pop-Location }
```

On macOS or Linux, use `./gradlew` in place of `./gradlew.bat`. The repository's [development guide](development.md)
has the Core and mod test commands, including the full game test runs.

The build uses a separate checkout of [Mycelium](https://github.com/worflor/mycelium), selected by `mycelium.lock`.
The maintained source wrapper needs Git and Java 17 or later. It creates the ignored `.mycelium/` checkout from the
locked commit and optional verified patch:

```powershell
.\mycelium.cmd prepare
```

On macOS or Linux use `./mycelium prepare`. The wrapper and its sources are checked in under `tools/mycelium/`;
no separate Mycelium release or Python installation is needed to prepare the compiler. An existing checkout is reused
only when it matches the recipe. Differing work is left untouched; use `--destination <fresh-path>` when needed.

Root and standalone loader builds verify the default source before loading the plugin. An absolute `MYCELIUM_HOME`
can select an editable development checkout. Compilation can use that override and reports a mismatch visibly.
The override may point to `.mycelium` itself while you develop there.
WorldPrompt chooses `"completion": "locked"`: `check` and jar assembly require captured source, including when an
override is set. Each jar records expected and actual compiler identities under `META-INF/mycelium/source.json`.

## Carrying unpublished Mycelium changes

Develop contracts and compiler improvements in the selected checkout or your own fork. After reviewing the source,
capture it with the repository and full base commit you intend others to fetch:

```powershell
.\mycelium.cmd capture --source C:\path\to\mycelium --repository https://github.com/your-account/mycelium.git --base <full-commit>
.\mycelium.cmd prepare --destination build/mycelium-review
$env:MYCELIUM_HOME = (Resolve-Path build/mycelium-review).Path
.\gradlew.bat build
```

Capture includes tracked changes, deletions and new non-ignored source, preserves the real Git index, and verifies the
recipe's round trip before replacing the lock. A content-addressed patch is included only when needed. Commit the
reviewed lock and optional patch with the consumer change. Ignored build outputs are excluded from source identity;
keep build inputs tracked. Changes left only in the ignored checkout cannot reach CI.

To update, run `mycelium.cmd update --revision <full-commit> --destination <fresh-path>`. This merges the captured work
with that revision in a separate Git checkout. It leaves the active source and lock alone, retaining conflicts for
normal Git resolution. Use `MYCELIUM_HOME` for focused compilation and game runs, then capture the candidate against
the requested new base. Prepare a fresh checkout and run the full build before handing it off. A clean fork commit
can be pinned without a patch. Updating the compiler does not require updating the independently versioned wrapper.

The wrapper's [README](../tools/mycelium/README.md) covers its maintained hooks and distribution. Upgrade the wrapper
from Mycelium's consumer bundle, keeping its source JAR and licenses together. The selected compiler's package and
proof rules still apply to contract work.

## The crossing workflow

1. Read the relevant WorldPrompt code and tests first. Keep changes in `common/`, `pure/` or the appropriate loader
   module unless a behavior needs a contract of its own.
2. When a real cross-target guarantee appears, inspect the selected Mycelium checkout's `AGENTS.md`,
   `docs/building-with-mycelium.md`, `CONTRACT-PACKAGE.md` and `BRIDGE-PACKAGE.md`. Read the candidate contract's
   `SPEC.md` and bridge manifest; use them only when their meaning fits.
3. If it does not fit, develop the needed contract or bridge in the Mycelium checkout used by WorldPrompt. State the
   behavior and its laws, include broken fixtures for each law and a no-op fixture, add native realizations for the
   supported targets, and use the result from WorldPrompt in the same change. Keep loader types in the bridge's loader
   overlays and Minecraft types out of the contract model.
4. Run the contract harness and relevant loader build or game tests. For a change intended to work on both loaders, run
   the root `check` so both targets are exercised. Keep user-facing documentation current, but do not copy build logs
   or verification transcripts into docs.

Mycelium is developed alongside its consumer. A missing contract is normal feature work in the selected checkout;
WorldPrompt does not need to wait for an upstream release before the crossing can be built and reviewed. For detailed
package and compiler rules, use the Mycelium docs in that checkout as the authority.

## Licensing

The repository's original WorldPrompt files retain their MIT license. Source imported from Mycelium carries its own
MPL-2.0 notice and obligations; retain that notice and follow its terms when distributing changes to those files.
Both license texts are included in each mod jar and its sources jar.
