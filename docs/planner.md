# Planner

How WorldPrompt Core turns a prompt and a captured block volume into a validated edit plan: the CLI providers, the planning passes, what the model sees and may call, the plan format, and every limit.

For where planning sits in a job, see [architecture](architecture.md). For how the plan reaches Minecraft, see [protocol](protocol.md).

## Overview

A planning job runs inside a spawned worker process (`world_worker.py`) and calls `pipeline.plan_volume`:

1. Core builds a summary of the captured volume, a terrain survey and preview images.
2. It runs one or two planner passes. Each pass is one bounded run of the user's Codex or Claude CLI, which may call a private set of offline tools and returns a complete plan as JSON.
3. Core validates the plan against the captured volume and the bundled block registry, then applies it to the source exactly once. That application compiles the replay that the mod places.

Core never gives the model access to Minecraft, files or the network, and never reads provider credentials. The model sees only the data below and the [planner tools](#planner-tools). Everything the model returns is checked again locally; an invalid final plan never reaches the mod.

## Providers

Core drives two CLIs through subscription sign-in: Codex CLI (`codex`) and Claude CLI (`claude`). There is no API client and no API-key setting. The `Provider` class in `providers.py` holds everything that differs between them; `agent.py` is the generic bounded-subprocess driver.

| | Codex | Claude |
|---|---|---|
| Login check | `codex login status` | `claude auth status` (JSON, must report `loggedIn` with `authMethod` `claude.ai`) |
| Sign-in command | `codex login` | `claude auth login` |
| Planning command | `codex -a never exec --sandbox read-only --ephemeral --json --output-schema ...` | `claude --print --restricted --input-format stream-json --output-format stream-json --json-schema ...` |
| Request input | `request.json` on stdin; previews as `--image` arguments | one streaming user message on stdin with base64 image blocks (PNG, JPEG, GIF or WebP) |
| Plan comes back as | the CLI's last message, written to `plan.json` | `structured_output` of the final `result` event |
| Usage comes from | the last `turn.completed` event | the `result` event's `usage` |
| Reasoning effort | `-c model_reasoning_effort=...` | `--effort ...` |
| Fast mode | `-c service_tier="fast"` | `fastMode` in `--settings`; only for Opus models |
| Model picker | JSON-RPC `model/list` against `codex app-server` | `initialize` control request in stream-json mode |

Both commands disable action tools, user config, session persistence and hooks, and expose only the private MCP server. The system prompt replaces the CLI default (`model_instructions_file` for Codex, `--system-prompt-file` for Claude). Claude is bounded to 12 turns per pass; reaching the limit fails the pass.

### Environment

`provider_environment` copies the environment without the variables that could switch a CLI from subscription sign-in to API billing, a third-party host or a nested session (`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `CODEX_API_KEY`, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_OAUTH_TOKEN`, the Bedrock/Vertex/Foundry switches, `CLAUDECODE` and related). Normal OS and home variables stay so the CLI finds its own saved login. A frozen build restores the original `LD_LIBRARY_PATH`; Claude also gets `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`.

A CLI that reports API-key or API authentication counts as not signed in.

### Command discovery

An empty command (`codex_command` / `claude_command` is `[]`, the default) is resolved at launch by `cli_discovery.py`:

1. the executable on `PATH`;
2. `~/.local/bin/<name>`;
3. on Windows, the Codex app (`%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe`) and Claude desktop caches (`%APPDATA%\Claude\claude-code\*\claude.exe`, plus the Microsoft Store package's cache);
4. VS Code, Insiders and Remote extensions (`openai.chatgpt-*`, `anthropic.claude-code-*`), newest first.

A non-empty command is an argument array used exactly as entered, with no discovery and no shell. It may hold an absolute path, a wrapper or extra arguments (at most 16 entries). Core on WSL can drive a Windows `.exe` CLI; see [architecture](architecture.md).

### Login check

`agent.check_provider` runs the status command with a 15 second limit, in its own process group (POSIX) or a kill-on-close Job Object (Windows), and returns the resolved command and a plain message. It never returns account identifiers or raw output and never starts or relays a sign-in: the user signs in through the CLI on the same machine and account as Core. A stored sign-in can still be expired or lack access to a model; planning is what proves that.

### Models and settings

`models.list_models` asks the CLI's own model picker for its models without starting a model turn (20 second timeout, 2 MB of output at most, hidden models skipped). Each entry has an `id`, `name`, `supports_images`, the model's `reasoning_efforts` and, when reported, a default effort and resolved model. Claude's account details in the same response are discarded.

Settings per provider (defaults from `config.py`):

| Key | Codex default | Claude default |
|---|---|---|
| `<provider>_command` | `[]` | `[]` |
| `<provider>_model` | `""` (CLI default) | `""` (CLI default) |
| `<provider>_reasoning_effort` | `medium` | `medium` |
| `<provider>_fast_mode` | `false` | `false` |

`provider` (default `codex`) selects the active one. Core accepts any lowercase effort token matching `[a-z][a-z0-9_-]{0,31}` and leaves the real list to the CLI; the GUI offers the levels each model reports. A model name is any string up to 128 characters. An empty model passes no `--model`, so the CLI picks its own default model, and jobs report it as `Codex default` or `Claude default`. Claude fast mode is rejected unless the model starts with `opus` or `claude-opus-`, so it needs an explicit model.

### Failures

A failed run is classified from the stderr tail and the structured error fields of the event stream (`error`, `turn.failed`, an `is_error` result, status codes), matched as whole words so ids and plan text cannot trigger a hint. The message is one of: usage limit reached, run the provider's sign-in command, model unavailable, CLI incompatible (update it), schema rejected, or "inspect the private job logs". Raw logs are never returned. No world blocks change on any failure.

## Planning passes

The job request's `effort` field selects the workflow: `normal` is Single pass and `high` is Double pass (the mod sends these for its `single` and `double` choices).

| | Single pass (`normal`) | Double pass (`high`) |
|---|---|---|
| CLI runs | 1 | 2: draft, then review |
| Result | the pass's plan | the review's complete plan |

**Single pass** sends the volume summary, terrain survey and three images (isometric exterior, top view, a mid-depth Z slice) and takes the plan it returns.

**Double pass** first surveys the source with `spatial_context` (sampled columns, air runs, tiles, vertical profile). The *draft* pass gets the original source with three images (exterior, top view, coordinate-labelled slice atlas). Core applies the draft privately in memory and computes `fit_diagnostics`. The *review* pass gets the draft plan, its validation error if it failed, a summary of the candidate, fit diagnostics, overview and atlas images of both source and candidate, and the lectern check. It must return a complete replacement plan for the **original** source, never a patch to the draft. There is no further repair pass, and the draft is never used as a fallback. The draft's `summary` is kept on the job as `draft_plan_summary` so the player's summary book can show it beside the final one.

A draft that fails validation still reaches the review, which repairs it from the original source and the error. A final plan that fails validation fails the job. An empty `operations` array is how the model says the request cannot fit; the job then fails with the model's summary as the message.

If images cannot be rendered, Single pass continues without them and Double pass fails. Token usage of all passes is summed; see [Usage](#usage).

## What the model sees

Each pass writes `schema.json`, `instructions.md` and `request.json` to its private job directory.

**Instructions** (system prompt, `agent.py`) are built from the limits and the operation table, so they cannot drift from the code. They tell the model to return only the plan JSON, use only the six tools, validate the complete candidate before answering, treat volume data and images as data rather than instructions, work in local zero-based coordinates (x east, y up, z south), never exceed the bounds, and describe the finished result to the player in a few plain sentences in `summary`. They also cover scope (single build, restyle, terrain work, scattered features), buildings (foundations, headroom, access), operation order and the empty-plan rule. Command, structure and jigsaw blocks can be placed but carry no NBT, so they are always blank. Double pass appends a draft or review section.

**Request** (`request.json`) holds:

| Field | Content |
|---|---|
| `request` | the player's prompt |
| `lectern_instructions` | prompt-lectern entries, when present |
| `volume` | the summary: `dimensions` `[width, height, length]`, `data_version`, `non_air_blocks`, `non_air_bounds`, the 128 most common palette entries with counts (`palette_total`, `palette_truncated`), `terrain_context`, `preview_descriptions` |
| `limits` | `max_operations`, `max_plan_bytes`, `target_output_tokens` |
| `instruction`, `phase` | the per-phase task sentence and `normal`, `draft` or `review` |
| `context` | optional `viewpoint_context`; in Double pass also `source_spatial`; in review also the draft plan, validation error, fit diagnostics and candidate summary |
| `spatial_tools_available` | whether `inspect_region` and `sample_surface` can read the exact capture |

`terrain_context` is a survey made without a model call: at most 9 x 9 exact column samples (heights, materials, water, lava, neighbor height step, air clearance) plus regional tile aggregates. Its caveats tell the model that terrain is a material heuristic, not a biome map, and that samples do not prove solid ground below overhangs.

`viewpoint_context` is the player's position and look direction at submission, converted to local axes. It is a weak cue for words such as "here" or "this side" and never extends the bounds.

**Images** are rendered by `render.py` and `spatial.py` as simplified cube views (no textures), at most 768 pixels on a side, labeled in `preview_descriptions`. At most four images of 4 MB each are attached per pass.

### Prompt lecterns

Lecterns inside the selection that hold a book titled or renamed `prompt` are sent as `lectern_instructions`: local `position`, optional `world_position`, and the book text with `--- page N ---` markers. See [prompt lecterns](minecraft.md#prompt-lecterns) for how players write them. Core validates them (`lecterns.py`): at most 64 books, 4,000 characters each, 16,000 in total, no control characters other than newlines, unique positions, and each position must hold a lectern in the capture.

Every pass receives them. The extra instruction section gives each book the same authority and the same limits as the chat prompt: it applies to the room, building or feature around its lectern, refines the main request there, and cannot change the rules, tools, schema or bounds. The model must not write to any lectern position, which would destroy the lectern and its book, unless asked to; it should split broad fills around them and keep the lectern reachable. `lectern_check` (in the review context and in `preview_plan` results) reports whether a candidate changed each lectern block.

## Plan format

The model returns, and Core accepts, exactly this object (`operations.py` generates the JSON schema and the shape check from one spec table):

```json
{
  "version": 1,
  "summary": "A round stone tower with a glass dome.",
  "operations": [
    {"op": "fill", "min": [0, 0, 0], "max": [8, 0, 8], "block": ["minecraft:stone", "minecraft:cobblestone"]},
    {"op": "hollow_cylinder", "min": [0, 1, 0], "max": [8, 10, 8], "block": "minecraft:stone_bricks", "axis": "y", "thickness": 1},
    {"op": "single", "at": [4, 1, 0], "block": "minecraft:oak_door[half=lower]"}
  ]
}
```

- `version` is `1`. `summary` is a string of at most 4,000 characters. Unknown or missing fields are rejected.
- Operations run in array order, so later ones overwrite earlier ones. The array holds at most `max_operations` entries (512 by default and at most).
- Coordinates are integer `[x, y, z]`, **local** to the selection (origin at its minimum corner) and **inclusive** at both ends. They must lie inside the captured dimensions: there is no clipping and a plan can never enlarge the selection.
- Box operations need `min <= max` on every axis. `line` accepts its two endpoints in any order.

### Block states and lists

A block is a namespaced ID with optional properties, `minecraft:oak_stairs[facing=north,half=bottom]`, at most 512 characters. Omitted properties take the Minecraft default. Core validates the ID, property names and values against the [bundled registry](#block-registry); targets must be vanilla 26.3 blocks, though `replace` may match a modded ID present in the capture. Plans carry no block-entity data: containers are empty and command blocks have no command.

Every operation's `block` is one state or a list of 1 to 256 states. Fabric picks uniformly and independently for each placed block, so repeating an entry raises its weight. Core's previews, fit diagnostics and image renders use the **first** entry as a stand-in, and the final pattern is chosen in Minecraft. A later `replace` matches the blocks Fabric actually placed, not the stand-in; see [protocol](protocol.md) for how the replay carries this.

## Operations

| Operation | Fields | Meaning |
|---|---|---|
| `fill` | `min`, `max`, `block` | Fill the cuboid. `minecraft:air` carves. |
| `single` | `at`, `block` | Place one block. |
| `replace` | `min`, `max`, `block`, `match`, `preserve_properties` | Within the box, replace blocks matching any `match` entry with `block`. |
| `hollow_box` | `min`, `max`, `block`, `thickness` | Paint the shell of the box, `thickness` blocks deep; the interior is unchanged. |
| `solid_ellipsoid` | `min`, `max`, `block` | Paint a solid ellipsoid inscribed in the box. |
| `hollow_ellipsoid` | `min`, `max`, `block`, `thickness` | The outer ellipsoid minus an inner one whose radii are smaller by `thickness`; the interior is unchanged. |
| `solid_cylinder` | `min`, `max`, `block`, `axis` | An elliptical cross-section inscribed in the box, extruded along `axis` (`x`, `y` or `z`) over the whole box. |
| `hollow_cylinder` | `min`, `max`, `block`, `axis`, `thickness` | An open-ended tube: the cylinder minus an inner one with radii smaller by `thickness`; interior and ends are unchanged. |
| `line` | `start`, `end`, `block` | A one-block-thick line between two points. |

Rules behind the table:

- `thickness` is an integer of at least 1. Shell operations paint only the shell and never clear the interior; use an `air` fill first to hollow a space, and a fill or `solid_cylinder` for a floor or cap of a tube.
- `hollow_ellipsoid` needs every box dimension larger than twice `thickness`; `hollow_cylinder` needs both cross-section dimensions larger than twice `thickness`.
- `match` is a list of 1 to 256 entries. A bare ID such as `minecraft:oak_log` matches every state of that block; an entry with properties matches that exact full state (omitted properties compared as defaults). Matching reads the volume as it was before the operation, so replacements never chain within one operation.
- `preserve_properties` is required. When `true`, the replaced block's properties carry over if the target block supports both the property name and value (a log's `axis` survives replacement with a crimson stem); unsupported ones are dropped and explicit properties in `block` win. When `false`, only the target state is used. Unknown or modded targets inherit nothing.
- Adding an operation means one `OpSpec` in `operations.py` plus its geometry in `core.apply_plan`, then the mod's placement. The schema, shape check and instruction text follow from the spec.

## Planner tools

The CLI starts a private stdio MCP server (`worldprompt-core planner-tools --request ...`, in `planner_tools.py` and `spatial_tools.py`) with six read-only tools. Calls are per pass and counted even when they fail.

| Tool | Arguments | Result |
|---|---|---|
| `list_blocks` | optional `query`, `offset`, `limit` (1 to 100, default 50) | Searchable, paginated block IDs with `total` and `next_offset`. |
| `get_block` | `block` | Accepted properties, values and defaults. Unknown IDs get suggestions. |
| `validate_plan` | `plan` | `valid` plus errors naming the operation and field. Checks IDs, states, schema, bounds and workload; not physics, support or design quality. Up to 32 state errors are collected. |
| `inspect_region` | `min`, `max` (local xyz), optional `target` | Exact states of a box, column or slice: a palette and indices ordered x fastest, then z, then y. |
| `sample_surface` | `min`, `max` (local `[x, z]`), optional `step` (1 to 64), `target` | Per column: highest non-air block, highest standable block, highest liquid and air cells above ground, with block palette indices. Exact, not interpolated. |
| `preview_plan` | `plan`, optional `include_image`, `camera` | Applies the complete plan privately to the **original** source and returns fit diagnostics, a report and, on request, a 768 x 544 image. |

`target` is `source` (the original capture, default) or `candidate` (the last successful `preview_plan` of this pass). A failed preview clears the candidate. In Double pass review the draft is separate: every tool preview replays on the original capture. `inspect_region` and `sample_surface` need the exact capture, written privately beside the request; they cannot read other files.

`preview_plan` without a camera renders a fixed isometric view from above the south-east. A `camera` (`position` as a local eye point within 4,096 blocks of the volume, `yaw` and `pitch` in Minecraft degrees, optional `fov` of 20 to 120, default 70) renders a perspective view and implies an image. An invalid plan or camera returns an error without spending a preview. Fit diagnostics are labeled heuristics: voxel and count facts, support and floor checks, spatial distribution and terrain surface comparison.

Budgets per pass, enforced by the tool server and quoted to the model from `limits.py` (as are the argument ranges above):

| Budget | Value |
|---|---|
| Tool calls | 32 |
| `preview_plan` calls | 2 |
| `inspect_region` | 4,096 blocks and 256 KiB per call |
| `sample_surface` | 4,096 columns and 256 KiB per call |

A failing call (bad arguments, snapshot I/O, a renderer error) returns an error result and the server keeps running. Tools cannot change the dimensions or any budget.

## Limits

Every limit below is a constant in `limits.py`; settings ranges are `SETTING_RANGES` there, and their defaults come from `config.py`. The Fabric mod mirrors the limits it checks in `CoreLimits.java`, kept equal by `tests/test_limits_sync.py`.

| Limit | Value |
|---|---|
| Operations per plan | 512 maximum and default; configurable 1 to 512 (`max_operations`) when job limits are on |
| Plan size, Single pass / Double pass | 131,072 / 262,144 bytes |
| Player prompt | 64,000 UTF-8 bytes |
| Volume context in the request: Single pass / Double draft / Double review | 64,000 / 128,000 / 512,000 bytes |
| Prompt lectern text | 64 books, 4,000 characters each, 16,000 in total |
| Summary | 4,000 characters |
| Block state string | 512 characters |
| Block list or `match` list | 256 entries |
| Selection volume | 4,000,000 blocks, at most 32,767 per axis |
| Plan workload | 80,000,000 block touches |
| Replay placement data | 64 MiB |
| CLI log (stdout plus stderr), Single / Double | 4,000,000 / 8,000,000 bytes |
| Output-token target, Single / Double | 6,000 / 10,000 (advisory) |
| Timeout per pass | None while job limits are off (default). When on: `timeout_seconds` (Single) or `high_timeout_seconds` (Double), at least 1 second with no maximum, default 3,600 |
| Images per pass | 4, each at most 4 MB |
| Claude turns per pass | 12 |
| Tool budgets | see [Planner tools](#planner-tools) |

The volume-context limit applies to the request minus the player's prompt and lectern text, which have their own limits, so any accepted prompt can be planned. Exceeding it fails before the CLI starts. The output-token target and timeouts are not spending caps. Each job keeps the settings it was submitted with. A pass that exceeds the timeout, the log or the plan size is killed and fails; the process group (POSIX) or Job Object (Windows) is terminated so no CLI descendant survives. Rebuilding an edit from history never calls a CLI.

## Usage

Each pass records the tokens its CLI reported, and a job sums its passes. `usage.token_usage` reports input, output and total tokens plus whether the figures are complete. Input and output are totals; cached and reasoning counts are subsets and are not added again. Codex already includes cached input in `input_tokens`; for Claude, cache creation and cache reads are folded into `input_tokens` once. Missing usage stays unknown instead of being estimated, and a total is marked incomplete while a job is running or if any pass did not report. Because the model may take several internal turns and tool calls per pass, one pass is not one model request.

## Block registry

Core bundles the Java Edition 26.3 block registry (1,286 block IDs, 35,723 states) so planning works offline. It is generated data in `src/worldprompt_core/schemas/`, shipped in the wheel and the packaged app:

| File | Content |
|---|---|
| `block-properties-26.3.json` | every block and its property names and allowed values |
| `block-defaults-26.3.json` | each block's default property values |
| `block-registry-source-26.3.json` | the server version, download URL, SHA-1, report SHA-256 and the counts |

`block_registry.py` loads them. Registry validity does not simulate Minecraft: a valid block may still need support, a neighbor or a paired half to stay in place.

Regenerate only from the official server's block report. With Java 25:

```bash
java -DbundlerMainClass=net.minecraft.data.Main -jar server.jar --reports
python scripts/import_block_report.py generated/reports/blocks.json version.json server.jar
```

`version.json` is the 26.3 entry from Mojang's version manifest. The script accepts only version id `26.3`, checks the server jar against the SHA-1 in that metadata, verifies that each block lists every property combination, and rewrites the three files.

## Adding a provider

Add a `Provider` subclass in `providers.py` and register it in `PROVIDERS`. The subclass supplies:

- `name`, `label`, `login_command`, `status_args` (appended to the CLI command to read login status);
- `build_command`: the full planning command, using the files in the job directory and `server_command` for the private MCP server;
- `prepare_input`: the file whose bytes go to stdin; set `keeps_stdin_open` and `is_final_event` for a streaming CLI;
- `read_result`: the plan (or `None` if the CLI writes `plan.json` itself) and usage, normalized so `input_tokens` is the pass's total input;
- `login_status` (override when exit code alone is not enough, and to reject API authentication), `failure_detail` if the default classification is not enough;
- `model_list_command`, `model_efforts` and `model_key` for the model picker.

The provider name is also listed outside `providers.py`, and a new one must be added to each:

| Where | What |
|---|---|
| `agent_types.py` | `PROVIDER_NAMES` (`config.py`, `pipeline.py` and `server.py` check and loop over it); fast-mode model rules in `validate_model_settings` |
| `config.py` | `default_config` keys (`<name>_command`, `_model`, `_reasoning_effort`, `_fast_mode`) |
| `cli_discovery.py` | install locations per provider |
| `models.py` | the model-list request sequence in `list_models` |
| `static/index.html`, `static/app.js` | provider selector, per-provider settings and message text |

Tests use small script fixtures that emit each CLI's stdout shapes, so no test needs an account. See [development](development.md) for running them.
