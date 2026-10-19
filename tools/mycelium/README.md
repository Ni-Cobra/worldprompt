# Mycelium source wrapper

This bundle keeps a consumer's editable Mycelium source reproducible. It requires Git and Java 17 or later.
The wrapper version is independent of the compiler revision in `mycelium.lock`.
The Gradle hooks require Gradle 7.5 or later. The selected compiler, build JVM and loader may require a newer version.

Unpack the consumer bundle at the mod's root. Keep the launchers and `tools/mycelium` in version control,
including the source JAR and licenses. The wrapper installs no toolchains and publishes nothing.
Merge the bundle's root `.gitattributes` entries with any existing attributes. Keep its
`tools/mycelium/.gitattributes`: binary patch bytes and digests must survive Windows checkouts unchanged.
Build a bundle from Mycelium with:

```sh
./gradlew -p gradle-plugin :source-wrapper:consumerBundle
```

Create the first lock from a reviewed Mycelium checkout:

```sh
./mycelium capture --source /absolute/path/to/mycelium --repository https://github.com/your-account/mycelium.git --base <full-commit>
./mycelium prepare
```

On Windows use `mycelium.cmd`. The first capture defaults to `"completion": "development"`.
Set it to `"locked"` in the lock when completion and distributable jars must match the committed recipe.

For Groovy settings, put `apply from: file('tools/mycelium/settings.gradle')` inside `pluginManagement`.
For Kotlin settings use `apply(from = file("tools/mycelium/settings.gradle"))` there.
In a loader subdirectory, use `../tools/mycelium/settings.gradle`.
The hook verifies the source before including its plugin and wires completion and artifact jars to the same verifier.
The hook sets the mod's `mycelium.root` to the verified checkout and rejects conflicting configuration.

The included `source-lifecycle.md` comes from Mycelium's maintained `docs/source-lifecycle.md`. It covers updates, conflicts,
source overrides, capture and the limits of Git source identity. Run `mycelium --help` for the command interface.

The executable JAR contains Gson 2.11.0 under Apache-2.0. Wrapper sources are MPL-2.0.
The adjacent source JAR and `LICENSES` directory accompany redistribution.
