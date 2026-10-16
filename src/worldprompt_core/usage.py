"""Report actual CLI token totals without counting nested categories twice."""
from __future__ import annotations


def _count(value):
    return value if type(value) is int and value >= 0 else None


def token_usage(job):
    """Missing usage is unknown, and totals during a job are explicitly partial.

    Codex's input/output counts already contain their cached/reasoning subsets.
    Double pass sums completed calls; a failed or running call may have unreported use.
    """
    calls = _count(job.get("model_calls")) or 0
    passes = job.get("plan_passes") or []
    # The per-pass data reveals missing usage that an aggregate alone could hide.
    usages = [item.get("usage", {}) for item in passes if isinstance(item, dict)]
    if not usages and isinstance(job.get("usage"), dict) and job["usage"]:
        usages = [job["usage"]]
    inputs, outputs, totals = [], [], []
    fully_reported = True
    for usage in usages:
        if not isinstance(usage, dict):
            usage = {}
        input_count, output_count = _count(usage.get("input_tokens")), _count(usage.get("output_tokens"))
        if input_count is not None:
            inputs.append(input_count)
        if output_count is not None:
            outputs.append(output_count)
        total = input_count + output_count if input_count is not None and output_count is not None else _count(usage.get("total_tokens"))
        if total is not None:
            totals.append(total)
        else:
            fully_reported = False
    finished_inference = job.get("status") in {"validating", "ready", "reconstructing", "completed", "failed"}
    complete = (finished_inference and bool(totals) and fully_reported
                and len(usages) >= calls and not job.get("usage_incomplete", False))
    return {"input_tokens": sum(inputs) if inputs else None,
            "output_tokens": sum(outputs) if outputs else None,
            "total_tokens": sum(totals) if totals else None,
            "complete": bool(complete), "model_calls": calls}
