---
name: mycelium
description: Develop WorldPrompt with its selected Mycelium checkout, including feature contracts, native bridges, compiler improvements and game verification.
---

# Working with Mycelium in WorldPrompt

Read the project's [guide](../../../docs/mycelium.md) and follow its current build layout. Treat WorldPrompt's own
behavioral guarantees as candidates for checked contracts in the selected Mycelium checkout. Keep feature composition
and native effects in `common/`, `pure/`, or the appropriate loader module. Choose contracts for their meaning and
useful laws; code being shared is not sufficient by itself.

## Find the current source of truth

First locate the Mycelium checkout selected by the build: inspect the actual build files, `mycelium.lock` and
`MYCELIUM_HOME`. If no selected checkout exists, run `./mycelium prepare` (`mycelium.cmd prepare` on Windows); it
fetches the locked commit and verifies the selected source against the lock. The lock can also carry a
content-addressed patch, which is how compiler work that is not upstream yet travels with the consumer change that
needs it. WorldPrompt pins a plain published commit whenever the source it needs is already there, so a lock with no
`patch` field is the normal case, not a missing step. Do not substitute upstream `HEAD`, change the lock, or invent a
revision as part of ordinary feature work.

The public project is [worflor/mycelium](https://github.com/worflor/mycelium); use the selected checkout for
version-specific API details. Read WorldPrompt's relevant package and tests, then read that checkout's `AGENTS.md`,
`docs/building-with-mycelium.md`, `CONTRACT-PACKAGE.md` and, when native integration is involved,
`BRIDGE-PACKAGE.md`. The selected contract's `SPEC.md`, bridge manifest and realization table define its current API
and supported targets. Do not copy a remembered contract inventory into this skill; discover it from the selected
checkout.

## Choose the seam

- Keep Core logic in Python and the existing HTTP boundary. Mycelium does not move planning into Minecraft.
- Put shared Minecraft behavior and its host in `common/`. Put JDK-only classes in `pure/`; that source set cannot
  import Minecraft or loader APIs.
- Put Fabric and NeoForge event registration and native API bindings in their own projects. Preserve access to each
  loader's full API.
- Express feature guarantees through Mycelium contracts when their laws can catch a real failure, including the
  guarantees first learned in WorldPrompt. Keep WorldPrompt's composition and policy declarations in WorldPrompt.
- Put reusable compiler and compatibility improvements in Mycelium itself, alongside the consumer change that needs
  them. Follow that checkout's contribution rules and run its gates; do not work around a missing capability by
  duplicating compatibility behavior in each loader.
- Add a bridge when the contract needs a native Minecraft realization. Keep the base implementation game-only and
  put loader-specific files in loader overlays that add files without replacing shared files.

Keep the consumer and its Mycelium crossing coordinated: define the model, rejection fixtures and harness; add native
realizations only for targets that have been inspected; import and use the result from WorldPrompt; then run the
contract harness and relevant mod builds or game tests. A supported-target row describes an implementation claim, so
do not extend it past the code and evidence available.

When a task changes the selected Mycelium source, follow the guide's [pin refresh procedure](../../../docs/mycelium.md#carrying-unpublished-mycelium-changes).
Use `mycelium capture --source <absolute-checkout> --repository <repository> --base <full-commit>`, then review
the lock and optional content-addressed patch. Prepare the result into a fresh destination and build against it. Changes left only in the ignored checkout cannot
reach CI. Keep a base revision change explicit. Use `mycelium update` for an isolated Git merge candidate. WorldPrompt requires
captured source for completion and jars; an explicit `MYCELIUM_HOME` permits focused compilation while developing.
Keep build inputs tracked: source identity excludes ignored outputs. Wrapper upgrades come from the maintained
Mycelium consumer bundle and remain independent of the compiler pin.

## Build and report

Read the Minecraft version and Java toolchain from the current build files and selected Mycelium era table. Use that
JDK; the compiler's release floor can differ from the mod's. The WorldPrompt root wrapper's `check` exercises Fabric
and NeoForge; use one loader wrapper for focused work. Run the selected contract's standalone harness while iterating,
then the consumer build or game tests that prove the native path. A pure contract harness does not prove runtime
behaviour.
