"""Shared, world-independent planning pipeline for captured bounded volumes.

Adapters own source capture and publication. The private Double pass candidate is never
returned: only the validated reviewed plan is replayed against the source.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

from .agent import AgentConfig, generate_plan
from .agent_types import PROVIDER_NAMES
from .providers import get_provider
from .config import default_config, job_limits
from .core import BlockVolume, apply_plan
from .limits import (HIGH_CONTEXT_BYTES, HIGH_LOG_BYTES, HIGH_OUTPUT_TOKEN_TARGET, HIGH_PLAN_BYTES, MAX_MESSAGE_CHARS,
                     MAX_PROMPT_BYTES, MAX_SUMMARY_CHARS, REVIEW_CONTEXT_BYTES)


@dataclass
class PlanResult:
    plan: dict
    edited: BlockVolume
    report: dict
    passes: list[dict]
    usage: dict
    elapsed_seconds: float
    model: str
    manifest: dict | None = None  # The compiled replay, when plan_volume was given a replay_dir.


def agent_config_for(config: dict, effort="normal") -> AgentConfig:
    """Model settings are independent of the Single pass/Double pass build workflow."""
    if effort not in ("normal", "high"):
        raise ValueError("Workflow must be Single pass (normal) or Double pass (high).")
    config = {**default_config(), **config}
    limits = job_limits(config)
    # One operation cap applies to every pass; Fabric accepts at most MAX_OPERATIONS.
    options = {"timeout_seconds": limits["timeout_seconds"], "max_operations": limits["max_operations"]}
    if effort == "high":
        options = {**options, "timeout_seconds": limits["high_timeout_seconds"],
                   "max_prompt_bytes": HIGH_CONTEXT_BYTES, "max_plan_bytes": HIGH_PLAN_BYTES,
                   "max_log_bytes": HIGH_LOG_BYTES, "max_output_tokens": HIGH_OUTPUT_TOKEN_TARGET}
    provider = config["provider"]
    if not isinstance(provider, str) or provider not in PROVIDER_NAMES:
        raise ValueError("Only Codex CLI and Claude CLI are supported. API providers are not supported yet.")
    command = config[provider + "_command"]
    if not isinstance(command, (list, tuple)):
        raise ValueError("The provider command must be an argument array.")
    return AgentConfig(provider=provider, command=tuple(command),
                       model=config[provider + "_model"], reasoning_effort=config[provider + "_reasoning_effort"],
                       fast_mode=config[provider + "_fast_mode"], **options)


def model_settings(config: AgentConfig) -> dict:
    """Public settings requested for a job; model aliases remain aliases.

    The CLI's own default model, which Core does not know, is reported as "<CLI> default".
    """
    model = config.model or get_provider(config.provider).label + " default"
    return {"provider": config.provider, "model": model,
            "reasoning_effort": config.reasoning_effort, "fast_mode": config.fast_mode}


def plan_volume(volume: BlockVolume, prompt: str, job_dir: Path, preview_dir: Path,
                agent_config: AgentConfig, effort="normal", *, progress=None,
                planner=None, viewpoint_context=None, lecterns=None, replay_dir=None) -> PlanResult:
    """Plan and validate an edit without requiring a file, player, or live world.

    ``model_calls`` in progress is the historical name for CLI planner passes:
    Single pass invokes the CLI once and Double pass twice. A provider may use more than one
    internal model turn per pass; Claude is bounded to ``MAX_CLAUDE_TURNS``. Usage includes all
    tokens reported by the CLI for that pass.

    ``progress(**fields)`` receives truthful lifecycle and usage updates. The
    planner owns its call directory, so ``job_dir`` must not yet exist for Single pass.
    ``viewpoint_context`` is optional normalized adapter data from submission;
    it supplements the captured source and never expands the editable bounds.
    ``lecterns`` are optional prompt-book entries (local position and text) that
    form location-specific parts of ``prompt``. Every planner pass receives them.

    The reviewed (or single-pass) plan is applied to the source exactly once. Without
    ``replay_dir`` that is a plain ``apply_plan``; with it, ``compile_replay`` writes
    the replay there and its application supplies ``PlanResult.edited`` and ``report``
    too, so a world job never applies its final plan a second time.
    """
    if effort not in ("normal", "high"):
        raise ValueError("Workflow must be Single pass (normal) or Double pass (high).")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Describe how you want the volume to change.")
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ValueError(f"The prompt is too long (maximum {MAX_PROMPT_BYTES // 1000} KB).")
    from .lecterns import lectern_check, validate_lecterns
    lecterns = validate_lecterns(lecterns, volume, (viewpoint_context or {}).get("selection_origin"))
    # Only pass the keyword when present, so planners that predate lecterns keep working.
    lectern_args = {"lecterns": lecterns} if lecterns else {}
    planner = planner or partial(generate_plan, source_volume=volume)
    progress = progress or (lambda **values: None)
    job_dir, preview_dir = Path(job_dir), Path(preview_dir)
    job_dir.parent.mkdir(parents=True, exist_ok=True)
    preview_dir.mkdir(parents=True, exist_ok=True)
    summary = volume.summary()
    passes = []

    def total_usage():
        totals = {}
        for item in passes:
            for key, value in item["usage"].items():
                if type(value) is int:
                    totals[key] = totals.get(key, 0) + value
        return totals

    def total_time():
        return round(sum(item["elapsed_seconds"] for item in passes), 3)

    manifest = None

    def apply_final(plan, message):
        """Apply the plan that will be returned: once, and as a replay when one is wanted."""
        nonlocal manifest
        progress(status="validating", message=message)
        if replay_dir is None:
            return apply_plan(volume, plan, max_operations=agent_config.max_operations)
        text = plan.get("summary")
        progress(status="validating", message="Compiling the final operations for reconstruction.",
                 summary=text[:MAX_SUMMARY_CHARS] if isinstance(text, str) else "")
        from .replay import compile_replay
        compiled = compile_replay(volume, plan, replay_dir, max_operations=agent_config.max_operations)
        manifest = compiled.manifest
        return compiled.edited, compiled.report

    def record_pass(result, phase):
        passes.append({"phase": phase, "usage": result.usage,
                       "elapsed_seconds": result.elapsed_seconds,
                       **model_settings(agent_config),
                       "operations": len(result.plan.get("operations", [])),
                       "output_target_exceeded": getattr(result, "output_target_exceeded", False)})
        progress(plan_passes=list(passes), usage=total_usage(), agent_seconds=total_time())

    from .render import PREVIEW_HEIGHT, PREVIEW_WIDTH, render_preview, render_slice, render_top
    previews = []
    preview_error = None
    progress(status="preparing", message="Reading dimensions, materials, and terrain…")
    from .spatial import terrain_context
    summary["terrain_context"] = terrain_context(volume)
    viewpoint = {"viewpoint_context": viewpoint_context} if viewpoint_context is not None else {}
    try:
        previews.append(render_preview(volume, preview_dir / "source.png", width=PREVIEW_WIDTH, height=PREVIEW_HEIGHT))
        previews.append(render_top(volume, preview_dir / "top.png"))
        previews.append(render_slice(volume, preview_dir / "section_z.png", axis="z", index=volume.dimensions[2] // 2))
        from PIL import Image
        for preview in previews:
            with Image.open(preview) as picture:
                picture.thumbnail((PREVIEW_WIDTH, PREVIEW_WIDTH))
                picture.save(preview)
        summary["preview_descriptions"] = ["source.png: exterior isometric cube preview from above the south-east (north, west and bottom faces hidden), X east / Y up / Z south", "top.png: highest non-air block in each column; X increases right, Z increases down", f"section_z.png: vertical X/Y slice at Z={volume.dimensions[2] // 2}, Y increases upward"]
    except Exception as exc:
        preview_error = str(exc)
        if effort == "high":
            raise ValueError("Double pass could not prepare the source views needed for spatial review.") from exc
    progress(dimensions=list(volume.dimensions), preview_warning=preview_error)

    initial_fit = draft_error = None
    if effort == "normal":
        progress(status="planning", message="The selected provider is planning the edit (Single pass).", model_calls=1)
        result = planner(summary, prompt, job_dir, agent_config, preview_paths=previews,
                         **({"context": viewpoint} if viewpoint else {}), **lectern_args)
        record_pass(result, "normal")
        if not result.plan.get("operations"):
            raise ValueError(result.plan.get("summary") or "The planner could not find a suitable edit within these dimensions.")
        final_message = "Applying operations and checking the result."
    else:
        from .spatial import spatial_context, fit_diagnostics, render_spatial_atlas, render_site_overview
        job_dir.mkdir()
        progress(message="Surveying terrain, sampled air clearances and cross-sections (Double pass).")
        source_spatial = spatial_context(volume)
        (job_dir / "source_spatial.json").write_text(json.dumps(source_spatial, indent=2), encoding="utf-8")
        source_atlas = render_spatial_atlas(volume, preview_dir / "source_atlas.png")
        source_overview = render_site_overview(previews[0], previews[1],
                                               preview_dir / "source_overview.png", label="ORIGINAL source")
        draft_images = [previews[0], previews[1], source_atlas]
        draft_labels = ["1: source.png, ORIGINAL source exterior", "2: top.png, ORIGINAL source top view", "3: source_atlas.png, ORIGINAL coordinate-labelled orthogonal slices"]
        summary["preview_descriptions"] = draft_labels
        progress(status="planning", message="Double pass · pass 1/2: planning terrain adaptation, layout and spatial fit.", model_calls=1)
        draft = planner(summary, prompt, job_dir / "draft", agent_config,
                        preview_paths=draft_images, phase="draft",
                        context={"source_spatial": source_spatial, "preview_descriptions": draft_labels, **viewpoint},
                        **lectern_args)
        record_pass(draft, "draft")
        draft_text = draft.plan.get("summary")
        progress(status="checking", message="Applying the draft in memory and inspecting its fit.",
                 draft_plan_summary=draft_text[:MAX_SUMMARY_CHARS] if isinstance(draft_text, str) else "")
        candidate = draft_report = None
        try:
            candidate, draft_report = apply_plan(volume, draft.plan, max_operations=agent_config.max_operations)
        except ValueError as exc:
            draft_error = str(exc)[:MAX_MESSAGE_CHARS]
        review_images = [source_overview, source_atlas]
        review_labels = ["1: source_overview.png, ORIGINAL exterior and whole-area top view", "2: source_atlas.png, ORIGINAL coordinate-labelled orthogonal slices"]
        if candidate is not None:
            initial_fit = fit_diagnostics(volume, candidate)
            (job_dir / "draft_fit.json").write_text(json.dumps(initial_fit, indent=2), encoding="utf-8")
            candidate_image = render_preview(candidate, preview_dir / "draft.png", width=PREVIEW_WIDTH, height=PREVIEW_HEIGHT)
            candidate_top = render_top(candidate, preview_dir / "draft_top.png")
            candidate_overview = render_site_overview(candidate_image, candidate_top,
                                                      preview_dir / "draft_overview.png", label="CANDIDATE draft")
            region = draft_report.get("changed_bounds")
            candidate_atlas = render_spatial_atlas(candidate, preview_dir / "draft_atlas.png", region=region)
            review_images += [candidate_overview, candidate_atlas]
            review_labels += ["3: draft_overview.png, CANDIDATE exterior and whole-area top view after applying draft to original", "4: draft_atlas.png, CANDIDATE orthogonal slices, focused on changed bounds; coordinates remain original-local"]
        review_context = {"source_spatial": source_spatial, "draft_plan": draft.plan,
                          "draft_validation_error": draft_error,
                          "draft_summary": candidate.summary() if candidate is not None else None,
                          "fit_diagnostics": initial_fit, "draft_report": {
                              key: value for key, value in (draft_report or {}).items() if key != "operation_counts"},
                          "preview_descriptions": review_labels,
                          "replay_target": "ORIGINAL source; return a complete replacement plan, not a patch", **viewpoint}
        if lecterns:
            review_context["lectern_check"] = lectern_check(volume, candidate, [item["position"] for item in lecterns])
        summary["preview_descriptions"] = review_labels
        progress(status="reviewing", model_calls=2,
                 initial_fit_diagnostics=initial_fit, draft_validation_error=draft_error,
                 message="Double pass · pass 2/2: reviewing and refining the rendered draft's spatial fit." if candidate is not None else "Double pass · pass 2/2: repairing the draft's validation failure.")
        result = planner(summary, prompt, job_dir / "review", replace(agent_config, max_prompt_bytes=REVIEW_CONTEXT_BYTES),
                         preview_paths=review_images, phase="review", context=review_context, **lectern_args)
        record_pass(result, "review")
        if not result.plan.get("operations"):
            raise ValueError(result.plan.get("summary") or "The Double pass review could not produce a fitting plan. No output was saved.")
        final_message = "Replaying the reviewed plan on the original source and checking the result."

    # Keep the authentic full plan in both modes, including overwrites and carving.
    (job_dir / "plan.json").write_text(json.dumps(result.plan, indent=2), encoding="utf-8")
    (job_dir / "usage.json").write_text(json.dumps({"effort": effort, "usage": total_usage(), "elapsed_seconds": total_time(), "passes": passes}, indent=2), encoding="utf-8")
    edited, report = apply_final(result.plan, final_message)
    if tuple(edited.dimensions) != tuple(volume.dimensions):
        raise ValueError("The edit changed volume dimensions; output was rejected.")
    if effort == "high":
        final_fit = fit_diagnostics(volume, edited)
        (job_dir / "final_fit.json").write_text(json.dumps(final_fit, indent=2), encoding="utf-8")
    return PlanResult(result.plan, edited, report, passes, total_usage(), total_time(), result.model, manifest)
