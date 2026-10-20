# Mycelium source lifecycle

A consumer selects the Mycelium checkout or fork that defines its compiler. The source wrapper records that choice,
checks it before Gradle loads the plugin, and helps move reviewed compiler changes onto a new upstream base. It does not
replace the consumer's normal build gates or make the complete build hermetic.

## Requirements and installation

The wrapper runs on Java 17 or newer and requires Git on `PATH`. These are build-host requirements. The Java version for
an old Minecraft runtime remains the value in `eras/minecraft.json`; it is separate from the Java used to run Gradle and
the wrapper.
The maintained Gradle hooks require Gradle 7.5 or later. The selected compiler, build JVM and loader may impose a newer
minimum; the hook's API floor does not establish compatibility for that complete build.

Path options also accept absolute `file:` URIs. The hooks use these for path arguments and read UTF-8 output, keeping
Windows command-line encoding from changing the selected checkout.

From the Mycelium checkout, build the independently shipped wrapper bundle with:

```text
./gradlew -p gradle-plugin :source-wrapper:consumerBundle
```

Copy the bundle's consumer template into the consumer repository. Keep `mycelium-wrapper.jar`, its source JAR, and the
license files together under `tools/mycelium/`, along with the maintained consumer hook. The template includes the
`mycelium` and `mycelium.cmd` launchers, `consumer.gradle`, and `settings.gradle`; use those maintained files instead of
writing another hook. The settings hook locates the selected checkout before Gradle loads `dev.woflo.mycelium` and
includes its `gradle-plugin` build. The consumer hook runs the source check from `check` and before producing artifact
JARs and `sourcesJar`. It follows the plugin's `artifactJars` selection, defaulting to `jar`; unrelated test archives
remain available during development.
It also sets `mycelium.root` to the verified checkout and rejects a conflicting value, so source imports and provenance
refer to the same compiler.

Merge the template's root `.gitattributes` entries with any existing attributes. Preserve the attributes under
`tools/mycelium/` so Git does not change patch bytes during a Windows checkout. The bundle includes this guide;
upgrading the wrapper refreshes it from the same maintained source.

The consumer's default checkout remains `.mycelium` at its repository root. Prepare it using the committed recipe before
the first Gradle build. For a new consumer without a lock, first use the capture command below to record a reviewed
Mycelium checkout, then prepare it:

```text
./mycelium prepare
```

An explicit absolute `MYCELIUM_HOME` selects an editable checkout, including `.mycelium` itself or a developer's fork.
The default path always requires locked source; the override enables deliberate development. `check` compares the
selected source with the lock and prints actual and expected identities when they differ. In `locked` mode that
mismatch fails completion. With an explicit `MYCELIUM_HOME` override, `development` completion allows an uncaptured
checkout and reports its measured identity. An explicit source override can also be supplied to the `check` command.

## The source recipe

The consumer commits `mycelium.lock` with schema `2`. It records the repository URL or path, a full lowercase 40-character
Git revision, an optional patch path and its SHA-256 digest, the effective Git tree identity, and a completion policy:
`locked` or `development`. The patch and digest appear together. A patch is content-addressed under
`tools/mycelium/patches/` by its digest. It represents the consumer's reviewed changes on top of the selected base
revision; `effective_tree` identifies the complete resulting source.

Local repository paths resolve from the consumer root, independently of the preparation destination. Capture records
them as absolute paths; URLs and SSH remotes keep their Git transport syntax. Prefer a fetchable repository URL for a
shared handoff, since an absolute local path belongs to that machine.

Capture the currently selected source against the intended base commit with:

```text
./mycelium capture --source ABSOLUTE_SOURCE --repository REPOSITORY_URL --base FULLCOMMIT
```

Replace the uppercase placeholders with an absolute checkout path, the repository URL, and the complete lowercase
40-character commit identity. Capture retains the existing completion policy. If no lock exists, it defaults to
`development`. Review the resulting lock and optional patch, then run `prepare` into a fresh destination and run the
consumer gates before treating the recipe as complete.

The source identity covers Git-visible source: tracked files and new files not excluded by the source checkout's
`.gitignore`. The project's `.gitignore` may exclude untracked generated outputs from a new checkout. User, global, and
`.git/info/exclude` rules cannot hide uncaptured source; tracked files are included even when marked ignored. To inspect
or capture, the wrapper uses Git with a temporary index for normalization. This can create Git objects in the source
repository, but it does not fetch or edit the real source files or index. Symlinks and submodules are unsupported in
source recipes.

## Prepare, check, capture, update

`prepare` fetches the locked revision and reconstructs the effective tree, including the optional patch. Its default
destination is `.mycelium`; pass `--destination DIR` to prepare a separate candidate. An existing destination is reused
only when it matches the lock. A differing destination is left alone, so choose a fresh path when preparing another
source.

Preparation stages the locked patch in the new checkout so tracked ignored files and file modes reconstruct correctly.
Use `git diff HEAD` there to review the complete captured change.

`check` reads the selected checkout and writes a JSON report when `--report FILE` is supplied. It does not fetch. The
same source check is wired into consumer `check` and JAR tasks. A locked policy rejects an uncaptured compiler there;
development policy allows it while reporting its measured revision and tree. The wrapper validates the effective tree
when the locked base object is available locally.

Run it directly, for example:

```text
./mycelium check --source ABSOLUTE_SOURCE --report build/mycelium/source.json
```

`capture` stores the selected source as a recipe on top of the requested base. It writes an optional binary Git patch
when the source differs from that base and updates `mycelium.lock` only after validating that the recipe reproduces the
effective tree. It preserves `completion`, or uses `development` for a new lock.

`update` creates an isolated candidate by merging a requested upstream revision with the currently captured source:

```text
./mycelium update --revision FULLCOMMIT --destination ABSOLUTE_FRESH_PATH
```

The destination must not exist. The active `.mycelium` checkout and `mycelium.lock` stay untouched. If Git reports
conflicts, the candidate remains available for ordinary Git resolution. Resolve them there, then test that checkout by
setting `MYCELIUM_HOME` to its absolute path and running focused compilation and game tasks. Locked completion still
requires captured source. When it is ready,
capture it with the requested upstream commit as `--base`, for example:

```text
./mycelium capture --source ABSOLUTE_CANDIDATE --repository REPOSITORY_URL --base FULLCOMMIT
```

The completion policy remains whatever the consumer already chose.

## Boundaries

One selected editable Mycelium checkout or fork is enough to author and use a feature contract or compiler change. No
upstream permission is required; an issue or contribution can be considered separately. Contract registry rules,
package rules, and evidence gates continue to apply. Read `docs/admission.md`, `CONTRACT-PACKAGE.md` and
`BRIDGE-PACKAGE.md` in the selected checkout; these paths refer to that source, including when this guide is read from
a consumer bundle.

The source check identifies the compiler tree selected by the consumer. Existing contract proofs are still interpreted
by the plugin's current fingerprint rules; the wrapper does not expand the proof verifier's coverage. The compiler's
other inputs and the full consumer build are not all fingerprinted by this lifecycle.
