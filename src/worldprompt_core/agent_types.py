"""Shared planning vocabulary: configuration, result and error types.

A leaf module (it imports only ``limits``) so providers, WSL interop, the settings
code and the subprocess driver in ``agent`` can all use it without cycles. ``agent``
re-exports everything here.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from .limits import MAX_CONTEXT_BYTES, MAX_LOG_BYTES, MAX_OPERATIONS, MAX_PLAN_BYTES, OUTPUT_TOKEN_TARGET

PROVIDER_NAMES = ("codex", "claude")
# Providers that restrict fast mode to some models (model-id prefixes).
_FAST_MODE_MODELS = {"claude": ("opus", "claude-opus-")}


class AgentError(RuntimeError):
    """An actionable planning failure; no world blocks were changed."""


def is_reasoning_effort(value) -> bool:
    """Effort levels come from each CLI's model list; Core only checks their shape."""
    return isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", value) is not None


def validate_model_settings(provider, model, reasoning_effort, fast_mode):
    """An empty model means the CLI's own default model."""
    if not isinstance(model, str):
        raise ValueError("The model must be a model ID, or empty for the CLI default")
    if not is_reasoning_effort(reasoning_effort):
        raise ValueError(f"Unsupported reasoning effort for {provider}")
    if type(fast_mode) is not bool:
        raise ValueError("Fast mode must be true or false")
    if provider == "claude" and fast_mode and not model.startswith(_FAST_MODE_MODELS["claude"]):
        raise ValueError("Claude fast mode requires a supported Opus model. Select Opus or turn fast mode off.")


@dataclass(frozen=True)
class AgentConfig:
    command: tuple[str, ...] = ()
    model: str = ""  # Empty: the CLI's own default model.
    timeout_seconds: float = 3600
    max_operations: int = MAX_OPERATIONS
    max_prompt_bytes: int = MAX_CONTEXT_BYTES
    max_plan_bytes: int = MAX_PLAN_BYTES
    max_log_bytes: int = MAX_LOG_BYTES
    max_output_tokens: int = OUTPUT_TOKEN_TARGET  # Requested target, not a hard CLI spending cap.
    reasoning_effort: str = "medium"
    provider: str = "codex"
    fast_mode: bool = False

    def __post_init__(self):
        if not isinstance(self.provider, str) or self.provider not in PROVIDER_NAMES:
            raise ValueError("Only Codex CLI and Claude CLI are supported. API providers are not supported yet.")
        if not isinstance(self.command, (list, tuple)) or not all(isinstance(v, str) and v for v in self.command):
            raise ValueError("command must be an argument array, for example ['codex']")
        validate_model_settings(self.provider, self.model, self.reasoning_effort, self.fast_mode)
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)) or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        for name in ("max_operations", "max_prompt_bytes", "max_plan_bytes", "max_log_bytes", "max_output_tokens"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_operations > MAX_OPERATIONS:
            raise ValueError(f"At most {MAX_OPERATIONS} operations are supported")


@dataclass(frozen=True)
class AgentResult:
    plan: dict[str, Any]
    usage: dict[str, int]
    elapsed_seconds: float
    model: str
    job_dir: str
    output_target_exceeded: bool = False
