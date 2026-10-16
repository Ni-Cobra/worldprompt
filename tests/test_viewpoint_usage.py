"""Optional capture-time view context and honest cumulative CLI token reporting."""
import copy
import json
import math
import multiprocessing
import sys

import pytest

from worldprompt_core.context import normalize_viewpoint
from worldprompt_core.usage import token_usage
from worldprompt_core.world_worker import run_worker
from test_world_jobs import submission, supervisor_factory, wait_status
from test_world_worker import FAKE_CLI, collect


def viewpoint(**updates):
    value = {"position": [104.25, 65.0, -38.5],
             "eye_position": [104.25, 66.62, -38.5],
             "selection_origin": [100, 60, -40],
             "yaw": 450.0, "pitch": 0.0,
             "look_direction": [-1.0, 0.0, 0.0],
             "dimension": "minecraft:overworld"}
    value.update(updates)
    return value


def test_viewpoint_maps_world_to_core_without_clamping_or_mutating_capture():
    raw = viewpoint()
    before = copy.deepcopy(raw)
    actual = normalize_viewpoint(raw)
    assert actual["position_local"] == [4.25, 5.0, 1.5]
    assert actual["eye_position_local"] == pytest.approx([4.25, 6.62, 1.5])
    assert actual["world_position"] == raw["position"]
    assert actual["world_eye_position"] == raw["eye_position"]
    assert actual["yaw_degrees"] == 90.0
    assert actual["pitch_degrees"] == 0.0
    assert actual["look_direction"] == [-1.0, 0.0, 0.0]
    assert raw == before
    assert normalize_viewpoint(None) is None
    outside = normalize_viewpoint(viewpoint(position=[-50.0, 100.0, -90.0]))
    assert outside["position_local"] == [-150.0, 40.0, -50.0]


@pytest.mark.parametrize("field,bad", [
    ("position", [True, 2, 3]), ("position", [1, 2]),
    ("eye_position", [1, math.inf, 3]), ("position", [1, math.nan, 3]),
    ("selection_origin", [1.1, 2, 3]), ("selection_origin", [True, 2, 3]),
    ("look_direction", [0, 0, 0]), ("look_direction", [10, 0, 0]),
    ("yaw", "90"), ("pitch", 91), ("pitch", -91),
    ("position", [10 ** 400, 0, 0]), ("yaw", 10 ** 400),
    ("dimension", "../world"), ("dimension", ""),
])
def test_invalid_viewpoint_rejected_before_owner_reserved(supervisor_factory, field, bad):
    supervisor = supervisor_factory()
    with pytest.raises(ValueError):
        supervisor.submit(submission(viewpoint=viewpoint(**{field: bad})))
    assert supervisor.list_jobs() == []


def test_submission_retains_immutable_viewpoint_and_ignores_spoofed_derived_context(
        supervisor_factory, tmp_path):
    supervisor = supervisor_factory()
    body = submission(viewpoint=viewpoint(), viewpoint_context={"position_local": [999, 999, 999]})
    before = copy.deepcopy(body["viewpoint"])
    job = supervisor.submit(body)
    body["viewpoint"]["position"][0] = 99999
    body["source"]["blocks"][0] = 1
    wait_status(supervisor, job["id"], "ready")
    captured = json.loads((tmp_path / "jobs" / "world" / job["id"] / "request.json").read_text())
    assert captured["viewpoint"] == before
    assert captured["viewpoint_context"]["position_local"] == [4.25, 5.0, 1.5]
    assert captured["source"]["blocks"] == [0] * 12


@pytest.mark.parametrize("effort,phases", [("normal", ["normal"]), ("high", ["draft", "review"])])
def test_real_worker_includes_captured_viewpoint_in_each_model_pass(tmp_path, effort, phases):
    fake = tmp_path / "fake.py"
    fake.write_text(FAKE_CLI)
    directory = tmp_path / "job"
    directory.mkdir()
    body = submission(effort=effort, viewpoint=viewpoint(),
                      source={"dimensions": [7, 6, 8], "palette": ["minecraft:air"], "blocks": [0] * 336})
    body["viewpoint_context"] = normalize_viewpoint(body["viewpoint"])
    path = directory / "request.json"
    path.write_text(json.dumps(body))
    config = {"codex_command": [sys.executable, str(fake), "ok"],
              "timeout_seconds": 20, "high_timeout_seconds": 20}
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=run_worker, args=(config, path, send))
    process.start()
    send.close()
    events = collect(process, receive)
    assert events[-1]["kind"] == "ready", events
    for phase in phases:
        call = directory / "planning" / phase if effort == "high" else directory / "planning"
        request = json.loads((call / "request.json").read_text())
        # The full normalized context must reach both Double pass calls, without
        # letting absolute world coordinates change the local operation bounds.
        assert request["context"]["viewpoint_context"] == body["viewpoint_context"]


def test_tokens_sum_calls_without_double_counting_cache_or_reasoning():
    job = {"status": "ready", "model_calls": 2, "plan_passes": [
        {"phase": "draft", "usage": {"input_tokens": 100, "cached_input_tokens": 70,
                                      "output_tokens": 30, "reasoning_tokens": 20}},
        {"phase": "review", "usage": {"input_tokens": 200, "cached_input_tokens": 140,
                                       "output_tokens": 40, "reasoning_tokens": 25}},
    ], "usage": {"input_tokens": 300, "cached_input_tokens": 210,
                 "output_tokens": 70, "reasoning_tokens": 45}}
    actual = token_usage(job)
    assert actual == {"input_tokens": 300, "output_tokens": 70, "total_tokens": 370,
                      "complete": True, "model_calls": 2}
    partial = token_usage({**job, "status": "reviewing", "model_calls": 3})
    assert partial["total_tokens"] == 370 and partial["complete"] is False


def test_unknown_partial_and_failed_usage_are_not_claimed_as_total():
    unknown = token_usage({"status": "planning", "model_calls": 1})
    assert unknown["total_tokens"] is None and unknown["complete"] is False
    missing_review = token_usage({"status": "failed", "model_calls": 2,
        "plan_passes": [{"phase": "draft", "usage": {"input_tokens": 100, "output_tokens": 30}}],
        "usage": {"input_tokens": 100, "output_tokens": 30}, "usage_incomplete": True})
    assert missing_review["total_tokens"] == 130 and missing_review["complete"] is False
    output_only = token_usage({"status": "completed", "model_calls": 1,
        "plan_passes": [{"phase": "normal", "usage": {"output_tokens": 30}}], "usage": {"output_tokens": 30}})
    assert output_only["input_tokens"] is None and output_only["complete"] is False
