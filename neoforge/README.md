# WorldPrompt NeoForge mod

This is the NeoForge artifact of WorldPrompt for Minecraft 26.3 and Java 25, built from
the same commands, capture, placement and undo/rebuild implementation as Fabric.

Install NeoForge 26.3.0.92 or newer for Minecraft 26.3, then place
`worldprompt-neoforge-1.0.0.jar` in the server's `mods` folder. Install only one WorldPrompt
artifact. Fabric API and a Mycelium runtime are not required. Players may connect with a
vanilla client; single-player uses the integrated server. Run WorldPrompt Core 1.0.0 on the
server machine under the same OS user.

The mod uses the same `config/worldprompt.json` and Core connection discovery as Fabric.
Its per-command `worldprompt.command.<name>` nodes use NeoForge's permission handler;
unset nodes retain the original operator defaults. NeoForge's permission API queries
players; non-player command sources use their effective vanilla default permissions.

From the repository root, run `./mycelium prepare`, then
`./gradlew :neoforge:build` (`.\gradlew.bat` on Windows). The jar appears in
`neoforge/build/libs/`. See [development](../docs/development.md),
[commands and configuration](../docs/minecraft.md), and
[developing with Mycelium](../docs/mycelium.md).
