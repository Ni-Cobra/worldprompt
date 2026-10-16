"""Every budget and limit Core enforces, plus the status vocabulary shared across modules.

The numbers the planner is told about (tool calls, previews, inspection sizes) come from
here, so the instruction text, the tool descriptions and the enforcing code cannot drift apart.
A leaf module: it imports nothing from this package. The Fabric mod mirrors the limits it
checks in ``CoreLimits.java``; ``tests/test_limits_sync.py`` keeps the two equal.
"""
from __future__ import annotations

# Planner tools: per pass, enforced by the private stdio server.
MAX_TOOL_CALLS = 32
MAX_PREVIEWS = 2
MAX_INSPECT_BLOCKS = 4096    # inspect_region: blocks per call
MAX_SURFACE_COLUMNS = 4096   # sample_surface: columns per call
MAX_SURFACE_STEP = 64        # sample_surface: column spacing
MAX_REGION_BYTES = 262_144   # inspect_region / sample_surface: response size (256 KiB)
MAX_BLOCK_QUERY_CHARS = 128  # list_blocks: search text
MAX_BLOCK_PAGE = 100         # list_blocks: IDs per page
MAX_CAMERA_DISTANCE = 4096   # preview_plan camera: eye distance from the volume, in blocks
MIN_CAMERA_FOV, MAX_CAMERA_FOV = 20, 120  # preview_plan camera: horizontal degrees
DEFAULT_CAMERA_FOV = 70
MAX_PLAN_ERRORS = 32         # validate_plan: registry errors reported before truncating

# Plan and prompt sizes.
MAX_OPERATIONS = 512         # Operations per plan; the Fabric mod accepts at most this many.
MAX_PLAN_BYTES = 131_072
MAX_SUMMARY_CHARS = 4000
MAX_STATE_CHARS = 512        # One block state string.
MAX_BLOCK_CHOICES = 256      # States in a block list or a replace match list.
MAX_PROMPT_BYTES = 64_000    # The player's prompt, in UTF-8 bytes.
MAX_CONTEXT_BYTES = MAX_PROMPT_BYTES  # Default budget for the volume context sent beside the prompt.

# Prompt lecterns (books titled "prompt" inside the selection).
MAX_LECTERNS = 64
MAX_BOOK_CHARACTERS = 4_000
MAX_TOTAL_LECTERN_CHARACTERS = 16_000

# Planner passes. Single pass uses the defaults; Double pass raises the budgets below.
MAX_LOG_BYTES = 4_000_000          # CLI stdout plus stderr.
OUTPUT_TOKEN_TARGET = 6_000        # Requested target, not a hard CLI spending cap.
HIGH_CONTEXT_BYTES = 128_000       # Double pass draft.
REVIEW_CONTEXT_BYTES = 512_000     # Double pass review.
HIGH_PLAN_BYTES = 262_144
HIGH_LOG_BYTES = 8_000_000
HIGH_OUTPUT_TOKEN_TARGET = 10_000
MAX_PREVIEW_IMAGES = 4             # Images attached to one pass.
MAX_PREVIEW_IMAGE_BYTES = 4_000_000
MAX_CLAUDE_TURNS = 12              # Claude CLI model turns per pass.

# Selections and placement.
MAX_AXIS = 32_767                  # Blocks along one axis of a selection.
MAX_BLOCKS = 4_000_000             # Blocks in a selection.
MAX_CAPTURE_PALETTE = 65_536       # Distinct states in a captured selection.
MAX_WORK = 80_000_000              # Block touches across all operations of a plan.
MAX_REPLAY_BYTES = 64 * 1024 * 1024  # Packed placement runs in one result.
MAX_WORLD_COORDINATE = 30_000_000  # Absolute horizontal Minecraft world coordinate.

# Core <-> mod HTTP.
MAX_WORLD_BODY = 64 * 1024 * 1024  # One request body from the mod.
MAX_MESSAGE_CHARS = 2000           # A job status or failure message.

# Numeric settings: inclusive (low, high) accepted by config.validate_config; None has no maximum.
SETTING_RANGES = {"port": (1, 65535), "world_port": (1, 65535),
                  "timeout_seconds": (1, None), "high_timeout_seconds": (1, None),
                  "max_operations": (1, MAX_OPERATIONS), "world_concurrency": (1, 16),
                  "world_max_pending": (1, 256), "world_lease_seconds": (10, 86400),
                  "world_max_jobs_per_player": (1, 256)}

# Job limits in force while the "job_limits" setting is off: values no local job reaches.
# The operation cap stays MAX_OPERATIONS, the most the Fabric mod accepts.
UNLIMITED_JOB_SETTINGS = {"timeout_seconds": 365 * 86_400, "high_timeout_seconds": 365 * 86_400,
                          "max_operations": MAX_OPERATIONS, "world_concurrency": 1_000_000,
                          "world_max_pending": 1_000_000, "world_max_jobs_per_player": 1_000_000}

# A world job that has reached one of these will not change again.
TERMINAL_STATUSES = frozenset({"completed", "failed"})

_NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}


def number_word(value: int) -> str:
    """Prose form of a small count for instruction text ('two'), digits beyond ten."""
    return _NUMBER_WORDS.get(value, str(value))
