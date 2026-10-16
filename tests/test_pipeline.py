"""The shared transaction accepts an in-memory volume without a file identity."""
import json
from types import SimpleNamespace

import pytest

from worldprompt_core.core import BlockVolume
from worldprompt_core.pipeline import agent_config_for, plan_volume


def test_in_memory_source_returns_complete_plan_and_progress(tmp_path):
    source = BlockVolume.empty((5, 4, 6))
    plan = {"version": 1, "summary": "Build, then carve.", "operations": [
        {"op": "fill", "min": [0, 0, 0], "max": [2, 2, 2], "block": "minecraft:stone"},
        {"op": "fill", "min": [1, 1, 1], "max": [1, 1, 1], "block": "minecraft:air"}]}
    events = []
    def planner(summary, prompt, directory, config, preview_paths):
        directory.mkdir()
        assert summary["dimensions"] == [5, 4, 6]
        assert len(preview_paths) == 3
        return SimpleNamespace(plan=plan, usage={"output_tokens": 10},
                               elapsed_seconds=1.5, model="fixture")
    result = plan_volume(source, "Build then carve", tmp_path / "call", tmp_path / "previews",
                         agent_config_for({}), progress=lambda **event: events.append(event), planner=planner)
    assert not source.non_air_mask().any()
    assert result.edited.non_air_mask().sum() == 26
    assert result.plan == plan
    assert json.loads((tmp_path / "call" / "plan.json").read_text()) == plan
    assert result.usage == {"output_tokens": 10}
    assert [event["status"] for event in events if "status" in event] == ["preparing", "planning", "validating"]


@pytest.mark.parametrize("effort", ["normal", "high"])
def test_high_is_a_workflow_not_a_reasoning_upgrade(effort):
    assert agent_config_for({}, effort).reasoning_effort == "medium"


@pytest.mark.parametrize("workflow", ["normal", "high"])
@pytest.mark.parametrize("provider,model,effort", [("codex", "gpt-6-astra", "xhigh"), ("codex", "gpt-6-astra", "ultra"),
                                                   ("claude", "opus", "max"), ("claude", "opus", "xhigh")])
@pytest.mark.parametrize("fast", [False, True])
def test_provider_settings_reach_cli_independently_of_workflow(tmp_path, workflow, provider, model, effort, fast):
    from worldprompt_core.agent import build_command
    config = agent_config_for({"provider": provider, provider + "_model": model,
                              provider + "_reasoning_effort": effort, provider + "_fast_mode": fast}, workflow)
    args = build_command(tmp_path, config)
    assert args[args.index("--model") + 1] == model
    if provider == "codex":
        assert f'model_reasoning_effort="{effort}"' in args
        assert ('service_tier="fast"' in args) == fast
        assert "--ignore-user-config" in args
    else:
        assert args[args.index("--effort") + 1] == effort
        assert json.loads(args[args.index("--settings") + 1]) == {"fastMode": fast, "disableAllHooks": True, "autoMemoryEnabled": False, "claudeMdExcludes": ["**"]}


@pytest.mark.parametrize("effort", ["normal", "high"])
def test_configured_operation_limit_rejects_longer_plans(tmp_path, effort):
    source = BlockVolume.empty((4, 4, 4))
    plan = {"version": 1, "summary": "Three markers", "operations": [
        {"op": "single", "at": [i, 0, 0], "block": "minecraft:stone"} for i in range(3)]}
    def planner(summary, prompt, directory, config, **kwargs):
        directory.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(plan=plan, usage={}, elapsed_seconds=0, model="fixture")
    with pytest.raises(ValueError, match="1–2 operations"):
        plan_volume(source, "markers", tmp_path / "call", tmp_path / "previews",
                    agent_config_for({"job_limits": True, "max_operations": 2}, effort), effort, planner=planner)
    result = plan_volume(source, "markers", tmp_path / "ok", tmp_path / "previews",
                         agent_config_for({"job_limits": True, "max_operations": 3}, effort), effort, planner=planner)
    assert len(result.plan["operations"]) == 3


@pytest.mark.parametrize("effort", ["normal", "high"])
def test_replay_dir_applies_the_final_plan_exactly_once(tmp_path, effort, monkeypatch):
    from worldprompt_core import core, pipeline, replay
    source = BlockVolume.empty((5, 4, 6))
    plan = {"version": 1, "summary": "Build, then carve.", "operations": [
        {"op": "fill", "min": [0, 0, 0], "max": [2, 2, 2], "block": "minecraft:stone"},
        {"op": "fill", "min": [1, 1, 1], "max": [1, 1, 1], "block": "minecraft:air"}]}
    draft = {"version": 1, "summary": "Draft.", "operations": plan["operations"][:1]}
    def planner(summary, prompt, directory, config, preview_paths, phase="normal", **kwargs):
        directory.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(plan=draft if phase == "draft" else plan, usage={}, elapsed_seconds=0, model="fixture")
    applied = []
    real = core.apply_plan
    def counting(volume, value, **kwargs):
        applied.append(value["summary"])
        return real(volume, value, **kwargs)
    monkeypatch.setattr(pipeline, "apply_plan", counting)
    monkeypatch.setattr(replay, "apply_plan", counting)
    events = []
    result = plan_volume(source, "Build then carve", tmp_path / "call", tmp_path / "previews",
                         agent_config_for({}, effort), effort, planner=planner,
                         progress=lambda **event: events.append(event), replay_dir=tmp_path / "replay")
    # The Double pass draft is applied privately for review; the final plan only once.
    assert applied == (["Build, then carve."] if effort == "normal" else ["Draft.", "Build, then carve."])
    expected, report = real(source, plan)
    assert result.report == report and result.manifest["operation_count"] == 2
    assert (tmp_path / "replay" / "manifest.json").is_file()
    assert result.edited.non_air_mask().sum() == expected.non_air_mask().sum() == 26
    assert [e["summary"] for e in events if "summary" in e] == ["Build, then carve."]
    # Only Double pass has a draft, and its summary is reported for the player's book.
    assert [e["draft_plan_summary"] for e in events if "draft_plan_summary" in e] == \
        ([] if effort == "normal" else ["Draft."])
    assert result.edited.blocks.tolist() == expected.blocks.tolist()


def test_without_replay_dir_no_replay_is_written(tmp_path):
    source = BlockVolume.empty((3, 3, 3))
    plan = {"version": 1, "summary": "One.", "operations": [
        {"op": "single", "at": [0, 0, 0], "block": "minecraft:stone"}]}
    def planner(summary, prompt, directory, config, preview_paths):
        directory.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(plan=plan, usage={}, elapsed_seconds=0, model="fixture")
    result = plan_volume(source, "One", tmp_path / "call", tmp_path / "previews", agent_config_for({}), planner=planner)
    assert result.manifest is None and not (tmp_path / "replay").exists()
