"""Real isolated workers and CLI children, using local fake inference only."""
import json
import multiprocessing
import os
import sys
import time

import numpy as np
import pytest

from worldprompt_core.core import BlockVolume, apply_plan
from replay_helpers import operation_runs
from worldprompt_core.world_worker import run_worker, snapshot_volume
from process_checks import process_is_running


FAKE_CLI = '''import json, os, pathlib, sys, time
mode = sys.argv[1]
args = sys.argv[2:]
request = json.load(sys.stdin)
output = pathlib.Path(args[args.index("--output-last-message") + 1])
phase = request["phase"]
output.with_name("fake-call.json").write_text(json.dumps({"pid": os.getpid(), "phase": phase, "args": args}))
if mode == "hang":
    time.sleep(60)
if mode == "fail_review" and phase == "review":
    print("fixture review failed", file=sys.stderr)
    sys.exit(1)
if phase == "draft":
    operations = [{"op": "fill", "min": [1, 1, 1], "max": [1, 1, 1], "block": "minecraft:gold_block"}]
else:
    operations = [
        {"op": "fill", "min": [4, 1, 1], "max": [4, 2, 2], "block": "minecraft:stone"},
        {"op": "fill", "min": [4, 1, 1], "max": [4, 1, 1], "block": "minecraft:air"},
        {"op": "fill", "min": [5, 1, 1], "max": [5, 1, 1], "block": "minecraft:oak_stairs[facing=north,half=bottom]"}]
if mode == "invalid_review" and phase == "review":
    operations[0]["max"] = [99, 2, 2]
output.write_text(json.dumps({"version": 1, "summary": phase + " complete fixture", "operations": operations}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 50}}))
'''


def start_worker(tmp_path, effort="normal", mode="ok", *, saved_settings=False):
    fake = tmp_path / "fake_cli.py"
    fake.write_text(FAKE_CLI)
    directory = tmp_path / "job"
    directory.mkdir()
    source = BlockVolume.empty((7, 6, 8), data_version=5000)
    source.blocks[0] = source.palette_id("minecraft:grass_block")
    payload = {"dimensions": list(source.dimensions), "palette": source.palette,
               "blocks": source.blocks.ravel().tolist(), "data_version": 5000}
    request_path = directory / "request.json"
    request_path.write_text(json.dumps({"source": payload, "prompt": "Build and carve a marker.", "effort": effort}))
    config = {"codex_command": [sys.executable, str(fake), mode],
              "timeout_seconds": 20, "high_timeout_seconds": 20}
    if saved_settings:
        from dataclasses import asdict
        from worldprompt_core.pipeline import agent_config_for
        config["_agent_config"] = asdict(agent_config_for(dict(config, codex_reasoning_effort="high", codex_fast_mode=True), effort))
        # Simulate later settings changes; the worker must use the saved job settings.
        config.update(codex_model="changed-model", codex_reasoning_effort="low", codex_fast_mode=False)
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=run_worker, args=(config, request_path, send))
    process.start()
    send.close()
    return process, receive, directory, source


def collect(process, receive):
    events = []
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            try:
                if receive.poll(.1):
                    events.append(receive.recv())
                if not process.is_alive():
                    # Drain after exit: the child can write between poll and exit.
                    while receive.poll():
                        events.append(receive.recv())
                    break
            except (EOFError, OSError):
                # Windows poll can raise BrokenPipeError after the final record.
                break
        process.join(timeout=2)
        assert process.exitcode == 0, events
        return events
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
        receive.close()
        process.close()


@pytest.mark.parametrize("effort,phases", [("normal", ["normal"]), ("high", ["draft", "review"])])
@pytest.mark.parametrize("saved_settings", [False, True])
def test_real_worker_shared_pipeline_and_authentic_final_replay(tmp_path, effort, phases, saved_settings):
    process, receive, directory, source = start_worker(tmp_path, effort, saved_settings=saved_settings)
    before = (directory / "request.json").read_bytes()
    events = collect(process, receive)
    assert events[-1]["kind"] == "ready", events
    assert not any(event["kind"] == "failed" for event in events)
    assert (directory / "request.json").read_bytes() == before
    captured = snapshot_volume(json.loads(before)["source"])
    np.testing.assert_array_equal(captured.blocks, source.blocks)
    plan = json.loads((directory / "replay" / "plan.json").read_text())
    assert plan == json.loads((directory / "planning" / "plan.json").read_text())
    assert plan["summary"] == phases[-1] + " complete fixture"
    assert len(plan["operations"]) == 3
    expected, report = apply_plan(source, plan)
    assert json.loads((directory / "report.json").read_text()) == report
    assert expected.palette[int(expected.blocks[1, 1, 1])] == "minecraft:air", "The private draft must never be applied"
    manifest = events[-1]["manifest"]
    assert manifest["operation_count"] == 3
    actual = source.blocks.copy().ravel()
    for index in range(3):
        runs = operation_runs(directory / "replay", index)
        for start, length, palette in runs:
            actual[start:start + length] = palette
        prefix, _ = apply_plan(source, {**plan, "operations": plan["operations"][:index + 1]})
        np.testing.assert_array_equal(actual, prefix.blocks.ravel())
    progress = [event["values"] for event in events if event["kind"] == "progress"]
    launched = [event["pid"] for event in events if event["kind"] == "process"]
    finished = [event["pid"] for event in events if event["kind"] == "process_finished"]
    assert len(launched) == len(phases) and all(pid > 0 for pid in launched)
    assert finished == launched
    assert [p["summary"] for p in progress if "summary" in p] == [phases[-1] + " complete fixture"]
    assert [p["status"] for p in progress if "status" in p] == (["preparing", "planning", "validating", "validating"] if effort == "normal" else ["preparing", "planning", "checking", "reviewing", "validating", "validating"])
    usage = [p for p in progress if "plan_passes" in p][-1]
    assert [p["phase"] for p in usage["plan_passes"]] == phases
    assert usage["usage"] == {"input_tokens": len(phases) * 100, "output_tokens": len(phases) * 50}
    for phase in phases:
        call_dir = directory / "planning" / phase if effort == "high" else directory / "planning"
        call = json.loads((call_dir / "fake-call.json").read_text())
        assert call["pid"] != os.getpid()
        reasoning = "high" if saved_settings else "medium"
        assert f'model_reasoning_effort="{reasoning}"' in call["args"]
        assert ('service_tier="fast"' in call["args"]) == saved_settings
        assert "--model" not in call["args"], "The default model and the saved settings leave the model to the CLI"
        assert all(p["reasoning_effort"] == reasoning and p["fast_mode"] == saved_settings for p in usage["plan_passes"])
        request = json.loads((call_dir / "request.json").read_text())
        assert request["phase"] == phase
        if phase == "review":
            assert request["context"]["draft_plan"]["operations"][0]["block"] == "minecraft:gold_block"
            assert request["context"]["draft_summary"]
            assert request["context"]["fit_diagnostics"]
            assert "ORIGINAL" in request["context"]["replay_target"]


@pytest.mark.parametrize("mode", ["fail_review", "invalid_review"])
def test_real_worker_review_failure_never_publishes_ready_or_draft(tmp_path, mode):
    process, receive, directory, source = start_worker(tmp_path, "high", mode)
    events = collect(process, receive)
    assert events[-1]["kind"] == "failed", events
    assert not any(event["kind"] == "ready" for event in events)
    assert (directory / "planning" / "draft" / "plan.json").is_file()
    assert not (directory / "planning" / "plan.json").exists()
    assert not (directory / "replay" / "manifest.json").exists()
    captured = snapshot_volume(json.loads((directory / "request.json").read_text())["source"])
    np.testing.assert_array_equal(captured.blocks, source.blocks)


def test_worker_cancellation_reaps_its_real_cli_child(tmp_path):
    process, receive, directory, _ = start_worker(tmp_path, mode="hang")
    call_path = directory / "planning" / "fake-call.json"
    try:
        deadline = time.monotonic() + 15
        while not call_path.exists() and time.monotonic() < deadline:
            if receive.poll(.05):
                receive.recv()
        assert call_path.exists()
        child_pid = json.loads(call_path.read_text())["pid"]
        assert process_is_running(child_pid)
        process.terminate()
        process.join(timeout=10)
        assert not process.is_alive()
        deadline = time.monotonic() + 5
        while process_is_running(child_pid) and time.monotonic() < deadline:
            time.sleep(.02)
        assert not process_is_running(child_pid)
        assert not (directory / "replay" / "manifest.json").exists()
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        receive.close()
        process.close()


@pytest.mark.parametrize("field,value", [("dimensions", [True, 2, 2]), ("dimensions", [32768, 1, 1]),
                                       ("palette", ["minecraft:air", "minecraft:air"]),
                                       ("blocks", [True]), ("blocks", [2]),
                                       ("data_version", -1)])
def test_worker_snapshot_rejects_invalid_source(field, value):
    payload = {"dimensions": [1, 1, 1], "palette": ["minecraft:air"], "blocks": [0]}
    payload[field] = value
    with pytest.raises(ValueError):
        snapshot_volume(payload)
