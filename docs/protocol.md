# Core and mod protocol

The contract between WorldPrompt Core (`world_server.py`, `world_jobs.py`, `replay.py`) and both mod loaders’ shared code (`CoreClient`, `CoreJobDriver`, `ReplayPayload`, `ReplayPlacer`). It is enough to write a compatible client or server. How Core runs jobs internally is in [architecture.md](architecture.md); the plan format is in [planner.md](planner.md).

All traffic is JSON over plain HTTP on loopback. The mod is the only client Core is designed for; Core never calls the mod.

## Connection

### Discovery

Core writes `connection.json` (mode `0600`) at startup and removes it on a clean exit:

```json
{"serviceUrl": "http://127.0.0.1:8768", "tokenFile": "/home/mc/.local/share/worldprompt/jobs/world/adapter-token.txt"}
```

| Platform | Location |
|---|---|
| Windows | `%LOCALAPPDATA%\WorldPrompt\connection.json` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/worldprompt/connection.json` (`XDG_DATA_HOME` is ignored unless absolute) |
| Either | `WORLDPROMPT_CONNECTION_FILE` overrides the path for both programs |

The mod re-reads the file and the token on every request (`AdapterConfig.connection`), so Core can start after Minecraft, restart, or change its port without a Minecraft restart. `tokenFile` must be an absolute path.

Explicit configuration in the mod's `config/worldprompt.json` takes precedence:

- A non-blank `tokenFile` or `token` disables discovery. The mod uses `serviceUrl` (default `http://127.0.0.1:8768`) and the token from that file or string.
- Otherwise the discovered `serviceUrl` is used, unless `serviceUrl` in the mod config differs from the default, in which case the configured one wins. The token always comes from the discovered `tokenFile`.

`serviceUrl` must be an HTTP(S) URL without credentials, query or fragment; trailing slashes are stripped. Config keys and defaults are in [minecraft.md](minecraft.md#configuration).

### Authentication

The adapter token is 32 random bytes, URL-safe base64, in a file created `0600` with `O_EXCL` and reused across restarts (default `<jobs_dir>/world/adapter-token.txt`, or the `world_token_file` setting). A valid token is at least 24 ASCII characters with no whitespace. Every request sends:

```
Authorization: Bearer <token>
```

Core rejects with `403` any request that:

- lacks the exact bearer token (compared in constant time),
- has a `Host` header that is not `localhost`, `127.0.0.1` or `::1` on the port the server listens on, or
- carries an `Origin` header or `Sec-Fetch-Site: cross-site` (browser requests are refused).

The listener binds `127.0.0.1` only, on `world_port` (default 8768). A client behind a proxy or on another host cannot satisfy the Host check by design. See [vps.md](vps.md) for token file modes when Core and Minecraft run as different users.

### Compatibility

Core and the mod share `MAJOR.MINOR`, and MINOR is this API (see [versioning](../README.md#versioning)); `PATCH` may differ. Both sides check this on every exchange, so a mismatched pair fails before any job starts:

- Every response from Core carries `X-WorldPrompt-Version: <Core version>`. The mod refuses a response whose version is missing, unreadable or of another `MAJOR.MINOR`, error replies included, and reports which side to install (`Versions.java`).
- Every request from the mod carries `X-WorldPrompt-Mod-Version: <mod version>`. Core answers a missing, unreadable or incompatible one with `409` and `{"error": "<what to install>", "code": "version_mismatch", "core_version": "<Core version>"}` (`world_server.py`). A submission refused this way fails at once, without retries.
- `GET /api/world/info` is answered whatever the mod's version, so `/wp info` can report both versions. Its reply carries Core's `version`.
- The result payload must carry `result_version: 2` and `run_encoding: "base64-u32le"`, or `ReplayPayload.parse` rejects it with "Update Core and the mod together".
- Unknown or malformed fields in a result fail the whole job before any block changes.

## HTTP conventions

- Responses are JSON objects with `Content-Type: application/json; charset=utf-8` and `Cache-Control: no-store`.
- `POST` requests need `Content-Type: application/json`, a `Content-Length` (no chunked bodies) and a non-empty JSON object body (`{}` is fine). The body limit is 64 MiB.
- Errors are `{"error": "<message>"}`, with `"code"` and `"limit"` added for the per-player limit, and `"code"` and `"core_version"` for a version mismatch.
- The mod sets a 5 s connect timeout and a 20 s timeout per request, accepts responses up to 8 MiB (128 MiB for `.../result`) and runs all HTTP off the server thread. Core drops an idle connection after 30 s.

| Status | Meaning |
|---|---|
| 200 / 202 | Success. `POST /api/world/jobs` answers 202. |
| 400 | Invalid input or state: malformed submission, queue full, Core shutting down, result requested before the plan is ready, bad application transition, `request_id` reused with different data. |
| 403 | Missing or wrong token, non-loopback Host, or browser-origin request. |
| 404 | Unknown route, or a job ID that is unknown (or not a UUID). |
| 409 | Per-player limit: `{"code": "player_job_limit", "limit": N}`. |
| 413 / 415 | Empty or oversized body / not `application/json`. |
| 500 / 503 | Internal error / job storage unavailable (the action was not acknowledged). |

## Endpoints

All paths are under `/api/world`. `{id}` is the job UUID, which the client chooses.

| Method and path | Request body | Response | Notes |
|---|---|---|---|
| `GET /info` | none | Core settings and load | Fields below. |
| `GET /jobs` | none | `{"jobs": [job, ...]}` | Unfinished jobs only, without `prompt`, `lecterns`, `summary`, `draft_plan_summary`, `selection_origin`, `dimension(s)`, `plan_passes`, `usage` and diagnostics. |
| `POST /jobs` | submission | job (202) | Idempotent per `request_id`. Starts the lease. |
| `GET /jobs/{id}` | none | job | Renews the lease. |
| `GET /jobs/{id}/result` | none | [result payload](#result-payload) | Only in `ready`, `reconstructing` or `completed`; otherwise 400. Not paged. |
| `POST /jobs/{id}/application` | `{"status", "operation", "message"}` | job | Reports placement progress. |
| `POST /jobs/{id}/cancel` | `{"message"}` | job | Idempotent. |

`/info` returns `version`, `provider`, `model`, `reasoning_effort`, `fast_mode`, `stopping`, `job_limits`, `active_jobs`, `queued_jobs`, `world_concurrency`, `world_max_pending`, `world_max_jobs_per_player`, `max_selection_blocks`, `max_operations`, `timeout_seconds` and `high_timeout_seconds`. The limits are the values in force; while `job_limits` is `false` they are unreachable values, and the mod shows them as no limits. It never exposes paths or commands.

### Submission

```json
{"request_id": "<uuid>", "owner_id": "<player uuid>", "owner_name": "Alice",
 "prompt": "a small stone tower", "effort": "normal",
 "source": {"dimensions": [w, h, l], "palette": ["minecraft:air", "..."], "blocks": [0, 0, 1, "..."], "data_version": 4901},
 "block_catalog": {"minecraft_version": "26.3", "blocks": {"minecraft:stone": {"properties": {}, "defaults": {}}, "mod:beam": {"properties": {"axis": ["x", "y", "z"]}, "defaults": {"axis": "y"}}}},
 "viewpoint": {"position": [x,y,z], "eye_position": [x,y,z], "look_direction": [x,y,z], "yaw": 0.0, "pitch": 0.0,
               "dimension": "minecraft:overworld", "selection_origin": [x, y, z]},
 "lecterns": [{"position": [x,y,z], "world_position": [x,y,z], "text": "..."}]}
```

| Field | Rules |
|---|---|
| `request_id`, `owner_id` | UUIDs. `request_id` becomes the job ID (a missing one is generated). |
| `owner_name` | 1 to 64 characters, no control characters. |
| `prompt` | Non-empty, at most 64,000 UTF-8 bytes (`MAX_PROMPT_BYTES`). |
| `effort` | `normal` (Single pass) or `high` (Double pass). Default `normal`. |
| `source` | `dimensions` are three integers 1 to 32767 with at most 4,000,000 blocks in total. `palette` is a list of unique block-state strings (1 to 65,536). `blocks` has one palette index per cell in the order below. `data_version` is an optional integer. |
| `block_catalog` | Optional snapshot from the running server. It contains a nonblank `minecraft_version` (at most 128 characters) and `blocks`. Each namespaced block maps to exactly `properties` (lowercase property names to nonempty unique value arrays) and `defaults` (one value for each property, included in its domain). Property values must match Minecraft 26.3's registered state-name syntax (`[a-z0-9_]+`). Each property's values are in the order the server's registry declares them, not sorted: a direction enum keeps its own order, and a numeric property stays in numeric order. Block IDs and property names are sorted, so the request bytes are deterministic. The catalog may contain vanilla and modded IDs. Core limits the UTF-8 JSON to 16 MiB, blocks to 65,536, properties to 64 per block and 1,000,000 total, and values to 256 per property and 4,000,000 total. A malformed catalog rejects submission. The mod leaves out any registered block it cannot represent within these limits and logs it, so the catalog it sends can be a subset of the server's registry; if nothing usable is left, or a total is exceeded, the mod omits the field entirely. If an older mod or offline caller omits it, Core uses its bundled 26.3 vanilla catalog. |
| `viewpoint` | Optional, but when present it needs exactly the keys shown. `look_direction` is a unit vector and `selection_origin` holds the world coordinates of the selection's minimum corner. |
| `lecterns` | Optional list of prompt lecterns inside the selection (limits in [planner.md](planner.md#prompt-lecterns)). `position` is local; the block there must be a lectern in `source`. |

Other keys are ignored. The catalog is part of the immutable job request and its idempotency fingerprint. Core does not return it in job or status responses or copy it into the model's request context; the private planner-tools subprocess reads it from a bounded sidecar. Cell order is x fastest, then z, then y: index `x + z * width + y * width * length`. Coordinates in plans, lecterns and runs are local to the selection and start at 0. Resubmitting an existing `request_id` with a byte-identical request returns the current job and renews its lease; different data returns HTTP 400. A request that would give one `owner_id` more unfinished jobs than `world_max_jobs_per_player` returns HTTP 409.

### Job object

```json
{"id": "<uuid>", "owner_id": "<uuid>", "owner_name": "Alice", "effort": "normal",
 "provider": "codex", "model": "...", "reasoning_effort": "medium", "fast_mode": false,
 "status": "planning", "message": "...", "queue_position": null,
 "operation": 0, "operation_count": 0, "elapsed_seconds": 12.4,
 "token_usage": {"input_tokens": null, "output_tokens": null, "total_tokens": null, "complete": false, "model_calls": 1}}
```

The mod reads `id`, `owner_id` (it checks both against the job it asked about), `status`, `message`, `queue_position`, `operation`, `operation_count`, `model`, `reasoning_effort`, `fast_mode` and `token_usage`. Core may add fields (`prompt`, `dimensions`, timestamps, per-pass usage); clients ignore what they do not know. `operation_count` is set when the job becomes `ready`. Usage semantics are in [planner.md](planner.md#usage).

## Job states

```
queued -> preparing -> planning -> [checking -> reviewing ->] validating -> ready
       -> reconstructing -> completed
any non-terminal state -> cancelling -> failed        (cancel while a worker runs)
any non-terminal state -> failed                      (error, cancel without a worker, lease expiry, Core restart)
```

`completed` and `failed` are terminal. Core drives everything up to `ready`; the client drives `reconstructing` and `completed` through `/application`:

| `status` | Allowed from | Requirements |
|---|---|---|
| `reconstructing` | `ready`, `reconstructing` | `operation` is an integer, not lower than the last reported value and at most `operation_count`. |
| `completed` | `reconstructing` | `operation` equals `operation_count`. |
| `failed` | `ready`, `reconstructing` | Records a failed application. The mod does not send it; it cancels instead. |

Repeating the status a terminal job already has is a 200; any other transition on a finished job is a 400. `message` (at most 2,000 characters) replaces the status text.

Every `GET /jobs/{id}`, application call and idempotent resubmission renews the job's lease. A non-terminal job idle for longer than `world_lease_seconds` (default 120) is cancelled. `cancel` fails a job without a worker immediately; with a worker it moves to `cancelling` and ends in `failed` once the worker and provider CLI are stopped (killed after 8 s). Core marks jobs it finds unfinished at startup `failed` and never resumes them.

## Mod sequence

`CoreJobDriver` advances each job once per server tick through these modes. `pollMilliseconds` (default 1000) is the heartbeat interval.

| Mode | Core call | Rule |
|---|---|---|
| CAPTURE | none | Reads the region and lecterns, `blocksPerTick` blocks per tick. |
| SUBMIT | `POST /jobs` | Retried after 2 s with the same `request_id` on any transient failure; at most 15 failed attempts in total, and 5 refused connections (about 10 s), then the job stops and the player is pointed to setup help. A 4xx other than 408 or 429 (including a full queue and `player_job_limit`) is final: "Not started". An unreadable connection file or token also ends the job at once. A reply naming another job or owner stops it. |
| WAIT | `GET /jobs/{id}` | Heartbeat. A failed poll only shows "Core connection interrupted". Moves on when `status` is `ready`; `failed` or `cancelling` fails the job; 404 means Core lost it. |
| RESULT | `GET /jobs/{id}/result` | One download, parsed and validated by `ReplayPayload`. Its dimensions must equal the captured selection. |
| PALETTE | heartbeat | Parses up to 256 palette entries per tick against the live block registry. |
| START | `POST .../application` `reconstructing`, `operation: 0` | Placement begins only after Core acknowledges. The undo entry is created here. |
| APPLY | heartbeat, plus `reconstructing` with the current `operation` | Places one operation at a time (see [placement](#placement)). Reports progress with each successful heartbeat. |
| FINISH | `POST .../application` `completed`, `operation: operation_count` | Retried every 2 s on transient failures. A permanent refusal (job gone, lease expired) is logged and the build still counts as complete locally. |

During RESULT, PALETTE, START and APPLY the mod stops writing and fails the job if Core has not answered for 15 s (`CONNECTION_GRACE_MS`), which stays inside Core's lease. A heartbeat showing `completed` before the mod reached FINISH also fails the job.

Cancelling or failing a submitted job sets the job to stop writing at once and loops `POST .../cancel` (one per poll interval) until Core reports `failed` or `completed`, or answers 404. The loop gives up after 60 s and releases the region locally; Core's lease then ends the job. On server shutdown the mod sends one cancel per submitted job and waits up to 2 s. A job that never reached SUBMIT is simply dropped.

Rebuilds make no Core calls. They restore the snapshot and replay the retained result from the PALETTE step on, with fresh random choices.

## Result payload

`GET .../result` returns the compiled replay in one response. Core builds it from `manifest.json`, `plan.json` and the per-operation `.runs` files on disk and adds the job's model fields.

```json
{"result_version": 2, "run_encoding": "base64-u32le", "version": 1,
 "dimensions": [6, 3, 4], "coordinate_order": "x + z * width + y * width * length",
 "palette": ["minecraft:air", "minecraft:stone", "..."],
 "operation_count": 4,
 "operations": [{"index": 0, "op": "fill", "run_count": 1, "writes": 24, "dynamic_match": false, "runs_base64": "AAAAABgAAAABAAAA"}],
 "plan": {"version": 1, "summary": "...", "operations": [{"op": "fill", "min": [0,0,0], "max": [5,0,3], "block": "minecraft:stone"}]},
 "provider": "codex", "model": "...", "reasoning_effort": "medium", "fast_mode": false,
 "draft_plan_summary": "...",
 "token_usage": {"total_tokens": 3250, "complete": true}}
```

- **`plan`** is the validated final plan exactly as the model produced and Core accepted it. Entry `i` of `operations` describes plan operation `i` (same `index` and `op`).
- **`draft_plan_summary`** is present only for Double pass: the `summary` of the draft plan (at most 4,000 characters), while `plan.summary` is the reviewed plan's. The mod shows both in the summary book.
- **`palette`** is the edited volume's palette: source states plus every state the plan wrote. For an operation whose `block` is a list, Core records the first entry.
- **`runs_base64`** is the base64 of `run_count` records of three little-endian unsigned 32-bit integers: `start`, `length`, `palette_id`. Each run writes `length` consecutive cells from the `start` cell index (the coordinate order above) with that palette state. Records are ascending and non-overlapping. `writes` is the sum of lengths. Every original operation keeps its own runs, including writes that equal the captured block, overwrites of earlier operations and carving, so replay reproduces authoring order.
- Core refuses to serve a result whose run files are truncated or have trailing data, or whose total run data exceeds 64 MiB.

### Validation by the mod

`ReplayPayload.parse` rejects the whole result unless:

- `result_version` is 2 and `run_encoding` is `base64-u32le`;
- each dimension is 1 to 32767 and the volume is at most 4,000,000;
- the palette has at most 65,536 + 512 * 256 entries;
- `operation_count` is 0 to 512 (`MAX_OPERATIONS`) and equals both the plan's and the packed list's length;
- each plan operation uses a known shape, has in-range coordinates, and its `block` (and `match` for `replace`) is a string or a list of 1 to 256 strings;
- `index` and `op` agree with the plan, the base64 length matches `run_count`, every run is inside the volume and palette, runs ascend without overlap, and the run lengths add up to `writes`;
- run data totals at most 64 MiB and `writes` at most 80,000,000.

### Placement

The mod places one original operation at a time, `blocksPerTick` blocks per tick (the whole replay in one tick at delay 0), pausing `/wp delay` ticks between operations. Every run cell is mapped to a world position from the selection's minimum corner.

- **Fixed block.** When `block` is a single string and the operation is not dynamic, the run's palette state is placed. A palette entry the live game cannot parse is skipped and counted.
- **Block lists.** When `block` is a list, the palette ID is ignored and the mod picks a random entry per placement. The legacy id `minecraft:chain` is mapped to `minecraft:iron_chain`.
- **Dynamic matching.** Core cannot know what the mod chose for earlier list blocks. A `replace` that follows any operation with a list `block` is flagged `dynamic_match: true`; its runs cover the entire box with palette ID 0. The mod places a block only where the live block matches the operation's `match` list. A match entry without brackets matches any state of that block. A bracketed entry is a complete state (omitted properties use vanilla defaults) and must equal the live block's properties exactly. Core sets the flag; the mod only checks that it equals what the plan implies.
- **`preserve_properties`.** For `replace`, a placed state copies the live block's properties (facing, waterlogged and so on) that the chosen entry does not set itself, where the new block supports them.

Blocks Minecraft refuses to place are skipped, counted and reported when the build finishes; they do not stop the build.

## Golden fixture

`fabric/src/test/resources/complete-result.json` is the committed result payload for a deterministic job: a 6 x 3 x 4 empty volume and a four-operation plan (a fill, a fill with a two-entry block list, a `replace` that therefore has `dynamic_match: true`, and a single torch), with fixed model fields, token usage and a Double pass draft summary.

- `tests/test_protocol_fixture.py` regenerates the payload with Core's real `compile_replay`, `read_complete_result` and `token_usage`, and fails if the committed file differs. It also asserts the features the mod relies on (version and encoding, a list block, the dynamic-match flags).
- `ReplayPayloadTest` in the mod's JUnit suite parses the same file with the real `ReplayPayload` and checks dimensions, palette, runs, flags and usage.

A change to the result format on either side breaks a test on both. After an intentional change:

```bash
WORLDPROMPT_UPDATE_FIXTURES=1 .venv/bin/python -m pytest tests/test_protocol_fixture.py
cd fabric && ./gradlew test
```

The fixture pins only the result payload. Endpoint behavior is covered by `tests/test_world_http.py` and `tests/test_complete_result.py` on the Core side, and by the mod's GameTests, which run against a scripted fake Core (`FakeCore`).
