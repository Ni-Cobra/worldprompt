# WorldPrompt Fabric mod

WorldPrompt lets you select a region in Minecraft, describe what should be there, and have your own Codex CLI or Claude CLI subscription plan the build. This mod is the Minecraft half: it captures your selection, talks to WorldPrompt Core on the same computer, and places the result block by block with undo and rebuild. It never talks to a model itself.

WorldPrompt 1.1.0 targets Minecraft 26.3, Java 25, Fabric Loader 0.19.5 and Fabric API 0.161.0+26.3. Commands and world edits run on the server, including single-player's integrated server. Players do not need the mod on their clients.

## Install

1. Install Fabric Loader and the matching Fabric API for Minecraft 26.3.
2. Close Minecraft or the server, remove older WorldPrompt jars from its `mods` directory, and put `worldprompt-fabric-1.1.0.jar` there.
3. Install and start WorldPrompt Core on the same computer and OS user as Minecraft (see the [project README](../README.md)), open its browser interface and sign in to Codex CLI or Claude CLI.
4. Start Minecraft. Look at a block and run `/wp select`, or set corners with `/wp pos1` and `/wp pos2`, then run `/wp prompt <description>`.

The mod creates `config/worldprompt.json` on first launch and finds Core automatically. On a dedicated server, `/wp prompt`, `/wp undo` and `/wp rebuild` need operator permission unless a permissions mod grants them.

## Compatibility

Use Core 1.1.0 with mod 1.1.0. Core and the mod share a MAJOR.MINOR version; if they differ, update both.
This jar is the Fabric artifact. The same shared implementation also builds for [NeoForge](../neoforge/README.md).

## Documentation

- [Commands, permissions and configuration](../docs/minecraft.md)
- [Core and mod protocol](../docs/protocol.md)
- [Building from source and tests](../docs/development.md)
