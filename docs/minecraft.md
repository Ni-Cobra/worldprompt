# Minecraft mod reference

How to use and configure WorldPrompt on Fabric or NeoForge: commands, selections, jobs, undo and rebuild, prompt lecterns, the summary book, in-game feedback, permissions and `config/worldprompt.json`. For installation see the [Fabric guide](../fabric/README.md) or [NeoForge guide](../neoforge/README.md); for the wire format between the mod and Core see [protocol.md](protocol.md).

Everything runs on the server (including single-player's integrated server). Players do not need the mod on their clients: selections, outlines, sounds and the summary book are vanilla packets and items.

## Commands

`/worldprompt` is an alias for `/wp`. The commands do not replace other mods' `/undo`.

| Command | What it does |
|---|---|
| `/wp pos1 [x y z]` | Sets the first corner. Without coordinates, uses the block you stand in. |
| `/wp pos2 [x y z]` | Sets the second corner, the same way. |
| `/wp select [sizeX sizeY sizeZ]` | Sets both corners around the block you look at. Three sizes (each at least 1) choose the exact box; without them the box is a cube of `selectionCubeSize` (default 30). |
| `/wp prompt [single\|double] <text>` | Captures the selection and starts a job. `single` (the default) is Single pass, `double` is Double pass; see [planner.md](planner.md#planning-passes). The text must be 1 to 16,000 characters. |
| `/wp delay <ticks>` | Sets the pause between plan operations for your later prompts and rebuilds, from 0 to 72,000 server ticks. Default 1. |
| `/wp undo [id]` | Restores the original blocks and block entities of an edit. |
| `/wp rebuild [id]` | Replays a retained plan locally, with no Core and no model call. |
| `/wp cancel [id]` | Cancels one of your unfinished jobs. |
| `/wp book [id]` | Gives you another copy of an edit's summary book. |
| `/wp jobs [page]` | Lists unfinished jobs, yours first, eight lines per page. |
| `/wp info` | Shows Core's and the mod's versions, provider, model, reasoning effort, fast mode, job counts and limits. |

`pos1` and `pos2` accept Minecraft's absolute, relative and local coordinates and report the corner and its dimension in chat. `delay` has no default value as a command: you must pass the number. `info` and `jobs` ask Core; they work for any player allowed to use them.

### Job IDs

Every job has a UUID. Chat, `/wp jobs` and the action bar show its first eight characters, the short ID. `undo`, `rebuild`, `cancel` and `book` accept the short or the full ID (case-insensitive) and tab-complete your own short IDs: running jobs for `cancel`, retained edits for the others. A short ID that matches more than one entry is rejected; use the full ID. Without an ID, `cancel` targets your newest job that is not already stopping, `undo` your newest edit that is not undone or still building, `rebuild` your newest edit that is not still building, and `book` your newest edit.

## Selections

A selection is two corners in one dimension. Each player has one, kept in memory until the server stops.

- `pos1` and `pos2` can be set in any order; the region is the inclusive box between them. Setting a corner in another dimension discards the other corner.
- `select` traces your view through loaded chunks, any distance, ignoring fluids, and stops at the first unloaded chunk (`Look at a block in a loaded chunk.`). The hit block is the center; with an even size on an axis, it is the upper of the two center blocks. The chat message lists both corners.
- The selection must fit the dimension's build height and contain at most `maxSelectionBlocks` blocks (default 2,000,000).
- A prompt needs both corners, in the dimension you are standing in. Core never expands the selection, so leave enough empty space above and around what you want built.
- A complete selection is outlined with faint white particles (see [Feedback](#feedback)); selections over `maxSelectionBlocks` are not outlined.

## Jobs

`/wp prompt` creates a job. It reads the whole selection on the server tick thread, at most `blocksPerTick` blocks per tick (default 4096), so a large capture spans several ticks. Every chunk of the selection must be loaded, otherwise the job stops with `Selection contains unloaded chunks. Keep the region loaded and retry.` Prompt lecterns are collected during the same pass (see [Prompt lecterns](#prompt-lecterns)). The mod then sends the captured volume to Core, waits while Core plans, downloads the complete result once, validates it, and places it. A job that fails validation changes no blocks.

A job moves through these stages, shown in the action bar and in `/wp jobs`: reading your selection, waiting to start (with the queue position), planning, checking the layout, reviewing and refining (Double pass only), preparing reconstruction, building (`Building · n/total` operations), finishing up.

### Placement and delay

The plan's operations are replayed in authoring order. With a positive delay (the default is 1), each operation starts at least `delay` ticks after the previous one finished, and writes at most `blocksPerTick` blocks per tick. With `/wp delay 0` the whole replay is placed in a single server tick, ignoring `blocksPerTick`; a large build can then stall that tick. Delays count server ticks, so they follow `/tick rate` and stretch when the server lags. The delay is per player, in memory, and applies to jobs and rebuilds you start afterwards.

When an operation lists several block states, the mod picks one at random per placement; `/wp rebuild` makes fresh random choices. Placement writes only the positions the plan writes. Overwritten block entities (chests, lecterns, signs) are replaced without dropping their contents, and `/wp undo` restores them.

The mod checks every state against the live game registry. A state it cannot parse is skipped, with the legacy `minecraft:chain` mapped to `minecraft:iron_chain`. The first five distinct warnings appear in chat, then a suppression notice, and the completion message reports the number of warnings and skipped placements.

### Concurrency and region reservation

A player can run several jobs at once, each on a selection that does not intersect another unfinished job. Every unfinished job reserves its whole selection from capture through cleanup: a prompt, undo or rebuild whose region intersects it (in the same dimension, even sharing one block) is refused with `Selection intersects ... active WorldPrompt job <id>. Wait for it to finish or cancel it first.` The one exception is `/wp undo` on your own job that is already stopping, since it no longer writes.

Core limits concurrency separately; those are Core settings, not mod settings, and apply only when **Apply job limits** is on in Core (off by default). `world_max_jobs_per_player` (default 4) caps unfinished jobs per player: an over-limit prompt reports `Not started` with Core's explanation and changes no blocks. `world_concurrency` (default 2) bounds how many planners run at once; further jobs wait in Core's queue in submission order. Rebuilds run in the mod and count toward neither. See [architecture.md](architecture.md) for how Core schedules jobs.

### Cancelling and failures

`/wp cancel` stops world writes at once and keeps asking Core to stop the planner. Cancelling a job that already placed blocks keeps its undo entry (`/wp undo <id>` is offered). Cancelling a rebuild restores the snapshot it started from, so it also needs the `undo` permission node.

If Core stays silent, the mod stops the job: while submitting, five refused connections or 15 failed attempts in total (about two seconds apart); while waiting for the plan, an unreachable Core is retried until it answers; from the result download on, when Core has not answered for 15 seconds. After a cancel or failure the mod asks Core to stop for up to 60 seconds, then releases the region locally and leaves it to Core's lease to end the job. Disconnecting stops all of the player's jobs. Shutting down the server asks Core to cancel every submitted job and waits up to two seconds for the answers. The exact retry rules are in [protocol.md](protocol.md).

## Undo and rebuild

The mod keeps an undo stack per player, newest first, holding at most `maxUndoEdits` edits (default 16, range 1 to 256); older edits are dropped and their snapshots released. An edit enters the stack when its job is about to place its first block. Each edit keeps the original blocks and block entities of its selection, the complete plan and the replay runs, all in memory: history is lost when the server stops, and it does not depend on Core afterwards.

The stack enforces these rules:

- `undo` and `rebuild` refuse an edit that is still building; cancel its job first.
- An edit cannot be undone or rebuilt while a later standing edit (yours or another player's) was captured over part of its region. Undo the later edit first.
- An undone edit can be rebuilt only while its region holds exactly the edits it was captured over; the rebuild is refused if another edit changed the region since, or one of those earlier edits has been undone or rebuilt.
- Undoing an edit that is already undone is refused. Use `/wp rebuild <id>`.
- A rebuild replaces its source edit on the stack. The new edit has the rebuild job's ID, and the source's IDs keep working as aliases.
- Before any restore (undo, a cancelled rebuild, the start of a rebuild) every chunk of the region must be loaded; otherwise the command is refused and nothing changes.

Undo restores the whole snapshot synchronously in one tick, which can be long for a large region. A rebuild first restores the snapshot (unless the edit is already undone), then replays the retained plan with the same model label and zero new tokens. Its progress is shown like any job, but it is not a Core job and does not appear in Core's history.

## Prompt lecterns

A lectern holding a prompt book inside the selection adds instructions for its own area, for example one lectern per room, so the chat prompt need not describe everything. The chat prompt is still required. A book counts when it is:

- a signed written book titled `prompt`;
- a written book or a book and quill whose custom name is `prompt`, for example after renaming it on an anvil; or
- any written book or book and quill whose text starts with the word `prompt`, such as `Prompt: stone counters and a furnace`.

Comparisons trim whitespace and ignore case. For a book detected by its text, the word must stand alone (`prompts` does not count) and is removed from the instructions together with an optional colon. Only lecterns inside the selection count.

Book text is the raw, unfiltered page text in page order. Blank pages are skipped; when more than one page remains, each starts with a `--- page N ---` line keeping its original page number. Tabs become spaces and other control characters are removed (`TextSanitizer`).

| Limit | Value |
|---|---:|
| Prompt lecterns per selection | 64 |
| Characters per book | 4,000 |
| Characters across all books | 16,000 |

Characters are Unicode code points. A selection over a limit stops the job after capture with a message naming the problem; nothing is truncated or sent. After capture the mod lists what it found, informationally, for example `a1b2c3d4 · 2 prompt lecterns: (12, 64, -30), (20, 64, -30)`. Empty prompt books are ignored and listed on a separate `Ignored empty prompt book` line. A lectern whose book has text but is not marked as a prompt is ignored with a yellow warning naming its position, in case the marker was forgotten, for example `The lectern at (14, 64, -30) holds a book not marked as a prompt, so it was ignored. If it is meant as one, start its first page with "prompt:".` Selections without lectern books print nothing extra.

Each book's text and position are sent to Core with the prompt; [planner.md](planner.md#prompt-lecterns) describes how the planner uses them and [protocol.md](protocol.md) the `lecterns` field. The planner is told to keep lecterns in place, and placement leaves untouched lecterns and their books alone. If a plan overwrites a lectern, the book goes with the block (it is not dropped), and `/wp undo` restores both. `/wp rebuild` replays the saved plan and does not read lecterns again.

## Summary book

When a prompt job completes, the mod gives its player a signed written book (a vanilla item). Rebuilds and undo give no new book.

- Title `WorldPrompt <short ID>`; author is the model ID Core reports, or `WorldPrompt` if none.
- The first pages hold the prompt, model, reasoning effort, Single or Double pass and token usage when Core reports it.
- The following pages hold the model's summary, or a note that it returned none. A Double pass book has two sections instead, each starting on a new page: `Draft`, the draft plan's summary, then `Review (built)`, the summary of the reviewed plan that was actually built.

Text is wrapped to the vanilla book screen (14 lines of 114 pixels, using conservative default-font glyph widths) and stops at 100 pages with an ellipsis. `§` formatting codes and control characters are removed. A full inventory drops the book at the player's feet. `/wp book [id]` gives another copy from the retained history. Set `summaryBook` to `false` to turn the automatic book off.

## Feedback

All chat messages are prefixed `[WorldPrompt]`, and job messages carry the job's short ID.

- **Chat** reports the start of a job (`WorldPrompt <id> · Single pass · capturing N blocks`, with a note when other jobs are running), prompt lecterns, token usage (a `+` marks a total that is incomplete, such as while a pass is still running), warnings, completion, failure and cancellation. Failures after blocks were placed name the `/wp undo <id>` that restores them.
- **Action bar** shows the status of your newest running job with a spinner and elapsed seconds, and with several jobs adds that job's short ID and `+N more`. Completion appears in chat and the action bar keeps showing any remaining job.
- **Outlines** are white dust particles along the twelve edges of a box. A running job outlines its captured bounds every 10 ticks, using up to `maxParticleSegments` samples per edge (default 96). An idle complete selection is drawn more faintly with a third of the samples every 20 ticks, unless it is exactly the region of one of your running jobs. Outlines go to the owning player only, and only while that player is in the selection's dimension.
- **Sounds** are note-block sounds. Stage changes (capturing, queued, planning, checking, reviewing, ready, reconstructing), completion, failure and undo each have their own sound, played to the job's owner. Placement plays a note-block bass drum at each operation's center (volume 2, pitch 1.5, with normal distance attenuation); an instant build plays it once at the selection center. Placement emits no particles.
- **Setup help.** When the mod cannot reach Core (missing or unreadable connection file or token, repeated refused connections, `/wp info` failing to connect) or failed to load its configuration, the error is followed by a clickable `Setup help` link to the [project page](https://github.com/Ni-Cobra/worldprompt).

`/wp info` prints, in order: Core's and the mod's versions and whether Core is connected or stopping; the configured provider with model, effort and fast mode; active and queued jobs, planners, capacity and per-player limit (or `no job limits`); the Single pass and Double pass timeouts (or `none`); and the selection limit in the mod and in Core. A Core whose `MAJOR.MINOR` differs from the mod's is reported with the version to install, for `/wp info` and for every job, which then fails before it starts. A reply the mod cannot read is reported as a probable version mismatch.

## Permissions

Every subcommand checks the node `worldprompt.command.<name>` through [fabric-permissions-api](https://github.com/lucko/fabric-permissions-api), which the mod bundles; LuckPerms or another permissions mod needs nothing else installed. The same nodes apply to the `/worldprompt` alias.

| Node | When the node is unset |
|---|---|
| `worldprompt.command.prompt` | Operators only (moderator level, 2) |
| `worldprompt.command.undo` | Operators only |
| `worldprompt.command.rebuild` | Operators only |
| `worldprompt.command.pos1`, `.pos2`, `.select`, `.delay`, `.book`, `.cancel`, `.jobs`, `.info` | Everyone |

A node set to true grants the command to non-operators; set to false it denies it even to operators. For example:

```
/lp group builder permission set worldprompt.command.prompt true
```

Every prompt runs on the Core operator's CLI subscription, so grant `prompt` only to players you trust with that usage. Cancelling a rebuild also needs `worldprompt.command.undo`. Undo and rebuild act only on the player's own retained edits. For running a dedicated server see [vps.md](vps.md).

## Configuration

The mod creates `config/worldprompt.json` with these defaults on first launch and reads it when the server starts; restart after editing. An invalid file (bad JSON, `tokenFile`/`token` not strings, a value out of range) leaves WorldPrompt unavailable, and commands answer `WorldPrompt is unavailable; check server configuration/logs.`

| Key | Default | Range | Meaning |
|---|---|---|---|
| `serviceUrl` | `http://127.0.0.1:8768` | HTTP(S) URL, no credentials, query or fragment | Core's address. |
| `tokenFile` | empty | absolute path | File holding Core's adapter token. |
| `token` | empty | | The adapter token inline. Prefer `tokenFile`. |
| `maxSelectionBlocks` | 2,000,000 | 1 to 4,000,000 | Largest selection the mod accepts. |
| `blocksPerTick` | 4096 | 1 to 65,536 | Blocks captured or placed per server tick (not with delay 0). |
| `pollMilliseconds` | 1000 | 250 to 10,000 | How often the mod polls Core for a job. |
| `maxParticleSegments` | 96 | 1 to 256 | Maximum outline samples per edge. |
| `selectionCubeSize` | 30 | 1 to 512 | Default edge of `/wp select` without sizes. |
| `maxUndoEdits` | 16 | 1 to 256 | Edits kept per player for undo and rebuild. |
| `summaryBook` | `true` | | Give the summary book after each completed prompt. |

Core accepts selections up to 4,000,000 blocks, so `maxSelectionBlocks` can only lower that limit.

### Connection

Leave `tokenFile` and `token` empty for automatic discovery. The mod re-reads Core's `connection.json` on every request, so Core can start after Minecraft or restart without a Minecraft restart, and a changed Core port is followed immediately. The file is at `%LOCALAPPDATA%\WorldPrompt\connection.json` on Windows and `${XDG_DATA_HOME:-~/.local/share}/worldprompt/connection.json` on Linux; `WORLDPROMPT_CONNECTION_FILE` overrides it for both programs. Discovery uses the URL and token path from that file; a `serviceUrl` other than the default takes precedence over the discovered URL. See [protocol.md](protocol.md) for the file contents and authentication.

A non-empty `tokenFile` or `token` disables discovery and uses `serviceUrl` as written, which is how to connect a Minecraft instance that cannot read Core's files or runs as a different OS user. The token path must be readable by Minecraft's operating system user (a Windows game cannot read a WSL path directly), and the token must not be shared. Core's **Connect a specific Minecraft installation** action in its GUI writes these entries for you and keeps a backup. If you later change Core's service port, an instance configured explicitly keeps the old URL until you connect it again and restart Minecraft.

Core and Minecraft must run on the same machine because both sides use loopback.
