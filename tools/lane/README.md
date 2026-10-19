# Fabric runtime lane

This lane drives a real Fabric client connected to a fresh dedicated server running WorldPrompt. Its client-only
hook sends commands through the logged-in player's connection, records messages the game receives, and probes blocks from
the client's synced world. The local Core fixture speaks the adapter protocol and returns a deterministic stone fill; it
never launches a model CLI or reads credentials.

All data goes under `fabric/build/lane/` by default. Set `WORLDPROMPT_LANE_DIR` to move it. The lane creates its own
server properties, config, world, logs, command stream, event telemetry, and report there. It uses a loopback ephemeral
port for each service. It does not access an installed Minecraft instance or stop Gradle daemons. `down` stops only the
recorded client, server, and fixture PIDs.

Run from the WorldPrompt checkout with Java 25 installed:

```powershell
python tools/lane/lane.py gen
python tools/lane/lane.py up
python tools/lane/lane.py smoke
python tools/lane/lane.py edges
python tools/lane/lane.py status
python tools/lane/lane.py down
```

`gen` compiles the client hook and uses Loom's `runClient` and `runServer` tasks to save their resolved launch arguments
under the lane directory. Ordinary `runClient` uses the main sources; the lane adds its hook when preparing its own launch.
The main mod build registers `src/lane/java` and `src/lane/resources` as the `worldprompt-lane` development mod;
that source set stays out of release jars. By default the client loads Fabric API and the lane hook without loading
WorldPrompt, while the server loads the development mod classes. The test hook starts only when
`-Dworldprompt.lane=<folder>` is supplied.

After building Fabric, pass the packaged artifact to exercise the server jar itself:

```powershell
python tools/lane/lane.py up --jar "C:\path\to\worldprompt-fabric.jar"
```

`--jar` verifies the embedded mod id and version and records the artifact SHA-256 in `pids.json`, `report.json`, and
`edges-report.json`. `--client-mod` also loads WorldPrompt in the client; omit it for the default client without the
WorldPrompt mod. With `--jar`, the artifact can be tested on both sides by combining the flags.

`smoke` asserts a real `/wp prompt` applies blocks to the dedicated server and that the client sees them. It then runs
`/wp undo` and `/wp rebuild`, checks the original and replayed blocks, captures player-visible feedback, and verifies
the fixture received one job total (rebuild must reuse saved replay data). Before the screenshots, it builds a small
stone-brick platform and quartz backdrop outside the selection, moves the real client player to spectator view, and waits
for the client hook to observe the expected camera position and direction plus the synced target block. Results and raw observations are written to
`report.json`, `core-metrics.json`, and `hook/events.jsonl`. Server, client, and fixture output are in `server.log`,
`client.log`, and `core.log`. The real client's framebuffer is saved after build, undo, and rebuild under
`hook/screenshots/`.

`edges` runs a deliberate Core planning failure and verifies the source blocks remain; removes the player's operator
status and checks that `/wp prompt` never reaches the fixture; then submits a held job, disconnects the real client, and
checks that the server cancels the job. It writes `edges-report.json`. Run it after `smoke`, since its final step leaves
the client disconnected.

The Fabric `up` mode proves the Fabric client/server path. Use the packaged NeoForge mode below for the independent
NeoForge server and vanilla-compatible-client path; loader GameTests remain a separate check.

## Packaged NeoForge server lane

The same vanilla-compatible Fabric client hook can also exercise the packaged NeoForge jar as a server-only mod. The
NeoForge run loads the jar from its isolated `mods/` folder, while the Fabric client loads Fabric API and the lane hook
without WorldPrompt installed. No development classes or GameTest classes are injected into this server run.

```powershell
python tools/lane/neoforge_lane.py gen
python tools/lane/neoforge_lane.py up --jar "C:\path\to\worldprompt-neoforge-1.0.0.jar"
python tools/lane/neoforge_lane.py smoke
python tools/lane/neoforge_lane.py edges
python tools/lane/neoforge_lane.py down
```

`gen` resolves a dedicated ModDev run and the minimal Fabric client config. `up` verifies the jar's NeoForge metadata,
records its SHA-256, stages it in a new NeoForge server directory, and starts only lane-owned Java processes. The same
world, command, block-sync, undo/rebuild, failure, permission, and disconnect assertions run against that artifact.
The NeoForge report includes `server_loader: neoforge` and the exact jar hash. The NeoForge server runtime is independent
of the client's Fabric loader; a passing lane is direct evidence the WorldPrompt client mod is not needed for server
commands or protocol behavior.
