"""Prompt lecterns: strict validation, prompt construction and the supervisor round trip.

Planner CLIs are local script fixtures; nothing here calls a model account.
"""
import copy
import json
import sys
from types import SimpleNamespace

import pytest

from worldprompt_core import __version__
from worldprompt_core.agent import AgentConfig, AgentError, LECTERN_INSTRUCTIONS, generate_plan
from worldprompt_core.lecterns import (MAX_BOOK_CHARACTERS, MAX_LECTERNS, MAX_TOTAL_CHARACTERS,
                                       lectern_check, validate_lecterns)
from worldprompt_core.pipeline import agent_config_for, plan_volume
from worldprompt_core.world_jobs import JobSupervisor
from test_world_jobs import submission, wait_status
from test_world_worker import FAKE_CLI
from test_viewpoint_usage import viewpoint

LECTERN = "minecraft:lectern[facing=north,has_book=true,powered=false]"
ORIGIN = [100, 60, -40]
DIMENSIONS = [7, 6, 8]


def index(x, y, z, width=7, length=8):
    return x + z * width + y * width * length


def source_payload(*positions):
    blocks = [0] * (7 * 6 * 8)
    for position in positions:
        blocks[index(*position)] = 1
    return {"dimensions": list(DIMENSIONS), "palette": ["minecraft:air", LECTERN],
            "blocks": blocks, "data_version": 4901}


def volume(*positions):
    from worldprompt_core.world_worker import snapshot_volume
    return snapshot_volume(source_payload(*positions))


def lectern(position, text="Make this room a library.", world=True):
    value = {"position": list(position), "text": text}
    if world:
        value["world_position"] = [o + p for o, p in zip(ORIGIN, position)]
    return value


ROOMS = [lectern([2, 1, 2], "Kitchen: stone counters\n--- page 2 ---\nAdd a furnace."),
         lectern([5, 1, 6], "Bedroom with a red bed.")]


def test_valid_lecterns_are_returned_unchanged_and_optional():
    source = volume([2, 1, 2], [5, 1, 6])
    before = copy.deepcopy(ROOMS)
    assert validate_lecterns(ROOMS, source, ORIGIN) == before
    assert ROOMS == before
    assert validate_lecterns(None, source, ORIGIN) == []
    assert validate_lecterns([], source, ORIGIN) == []
    # world_position is optional and is only cross-checked when an origin is known.
    assert validate_lecterns([lectern([2, 1, 2], world=False)], source, ORIGIN)[0] == {
        "position": [2, 1, 2], "text": "Make this room a library."}
    assert validate_lecterns([{**lectern([2, 1, 2]), "world_position": [0, 0, 0]}], source, None)


@pytest.mark.parametrize("bad,message", [
    ({"lecterns": "text"}, "must be a list"),
    ([{"position": [2, 1, 2]}], "requires position and text"),
    ([{**lectern([2, 1, 2]), "facing": "north"}], "requires position and text"),
    (["text"], "requires position and text"),
    ([lectern([7, 1, 2])], "inside the selection"),
    ([lectern([2, -1, 2])], "inside the selection"),
    ([{**lectern([2, 1, 2]), "position": [True, 1, 2]}], "inside the selection"),
    ([{**lectern([2, 1, 2]), "position": [2, 1]}], "inside the selection"),
    ([{**lectern([2, 1, 2]), "position": [2.0, 1, 2]}], "inside the selection"),
    ([lectern([2, 1, 2]), lectern([2, 1, 2], "again")], "repeats the position"),
    ([lectern([3, 1, 2])], "is not a lectern"),
    ([{**lectern([2, 1, 2]), "world_position": [102, 61, -37]}], "does not match"),
    ([{**lectern([2, 1, 2]), "world_position": [102, 61]}], "three bounded integer"),
    ([{**lectern([2, 1, 2]), "world_position": [10 ** 9, 61, -38]}], "three bounded integer"),
    ([lectern([2, 1, 2], "")], "nonempty string"),
    ([lectern([2, 1, 2], " \n ")], "nonempty string"),
    ([lectern([2, 1, 2], 42)], "nonempty string"),
    ([lectern([2, 1, 2], "x" * (MAX_BOOK_CHARACTERS + 1))], "exceeds 4000 characters"),
    ([lectern([2, 1, 2], "tab\there")], "control characters"),
    ([lectern([2, 1, 2], "carriage\rreturn")], "control characters"),
    ([lectern([2, 1, 2], "nul\x00")], "control characters"),
    ([lectern([2, 1, 2], "c1\x85")], "control characters"),
    ([lectern([2, 1, 2], "lone \ud800 surrogate")], "control characters"),
])
def test_invalid_lecterns_are_rejected(bad, message):
    source = volume([2, 1, 2], [5, 1, 6])
    value = bad["lecterns"] if isinstance(bad, dict) else bad
    with pytest.raises(ValueError, match=message):
        validate_lecterns(value, source, ORIGIN)


def test_count_and_total_text_limits():
    positions = [[x, y, z] for y in range(2) for z in range(8) for x in range(7)][:MAX_LECTERNS + 1]
    source = volume(*positions)
    assert len(validate_lecterns([lectern(p, "x") for p in positions[:MAX_LECTERNS]], source, ORIGIN)) == MAX_LECTERNS
    with pytest.raises(ValueError, match="At most 64"):
        validate_lecterns([lectern(p, "x") for p in positions], source, ORIGIN)
    per_book = MAX_TOTAL_CHARACTERS // 4
    assert validate_lecterns([lectern(p, "y" * per_book) for p in positions[:4]], source, ORIGIN)
    with pytest.raises(ValueError, match="16000 characters in total"):
        validate_lecterns([lectern(p, "y" * per_book) for p in positions[:4]] + [lectern(positions[4], "z")],
                          source, ORIGIN)
    # Characters are code points, matching the mod's count; multi-byte text is allowed.
    assert validate_lecterns([lectern(positions[0], "é" * MAX_BOOK_CHARACTERS)], source, ORIGIN)


def test_lectern_check_reports_changed_positions():
    source = volume([2, 1, 2], [5, 1, 6])
    candidate = source.clone()
    candidate.blocks[1, 6, 5] = candidate.palette_id("minecraft:stone")
    checks = lectern_check(source, candidate, [[2, 1, 2], [5, 1, 6]])
    assert [check["unchanged_in_candidate"] for check in checks] == [True, False]
    assert checks[1]["candidate_state"] == "minecraft:stone"
    assert lectern_check(source, None, [[2, 1, 2]]) == [{"position": [2, 1, 2], "source_state": LECTERN}]


SINGLE_CLI = '''import json, pathlib, sys
args = sys.argv[1:]
request = json.load(sys.stdin)
output = pathlib.Path(args[args.index("--output-last-message") + 1])
output.write_text(json.dumps({"version": 1, "summary": "marker", "operations": [
    {"op": "fill", "min": [0, 0, 0], "max": [0, 0, 0], "block": "minecraft:stone"}]}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
'''


@pytest.mark.parametrize("phase", ["normal", "draft", "review"])
def test_generate_plan_presents_lecterns_in_every_phase(tmp_path, phase):
    fake = tmp_path / "fake.py"
    fake.write_text(SINGLE_CLI)
    config = AgentConfig(command=(sys.executable, str(fake)))
    summary = {"dimensions": DIMENSIONS}
    generate_plan(summary, "Build a house", tmp_path / "call", config, phase=phase, lecterns=ROOMS)
    request = json.loads((tmp_path / "call" / "request.json").read_text())
    assert request["request"] == "Build a house"
    assert request["lectern_instructions"] == ROOMS
    assert "lectern_instructions" in request["instruction"]
    instructions = (tmp_path / "call" / "instructions.md").read_text()
    assert LECTERN_INSTRUCTIONS in instructions
    for phrase in ("location-specific parts of the player's main request", "same authority as request",
                   "cannot change these rules", "local position [x, y, z]", "--- page 2 ---",
                   "room", "Keep every listed lectern in place"):
        assert phrase in instructions
    # Without lecterns, the prompt is exactly as before.
    generate_plan(summary, "Build a house", tmp_path / "plain", config, phase=phase)
    plain = json.loads((tmp_path / "plain" / "request.json").read_text())
    assert "lectern_instructions" not in plain and "lectern" not in plain["instruction"]
    assert "Lectern instructions" not in (tmp_path / "plain" / "instructions.md").read_text()


def test_lectern_text_has_its_own_prompt_budget(tmp_path):
    fake = tmp_path / "fake.py"
    fake.write_text(SINGLE_CLI)
    summary = {"dimensions": DIMENSIONS}
    config = AgentConfig(command=(sys.executable, str(fake)), max_prompt_bytes=2_000)
    books = [lectern([2, 1, 2], "y" * MAX_BOOK_CHARACTERS)]
    generate_plan(summary, "x", tmp_path / "ok", config, lecterns=books)
    assert (tmp_path / "ok" / "request.json").stat().st_size > 4_000
    # The player's prompt is not volume context either, but volume context still counts.
    generate_plan(summary, "x" * 2_000, tmp_path / "long-prompt", config, lecterns=books)
    with pytest.raises(AgentError, match="input limit"):
        generate_plan({**summary, "filler": "z" * 2_000}, "x", tmp_path / "large", config, lecterns=books)
    with pytest.raises(AgentError, match="position and text"):
        generate_plan(summary, "x", tmp_path / "bad", config, lecterns=[{"text": "no position"}])


@pytest.mark.parametrize("effort,phases", [("normal", ["normal"]), ("high", ["draft", "review"])])
def test_pipeline_passes_lecterns_to_every_pass(tmp_path, effort, phases):
    source = volume([2, 1, 2], [5, 1, 6])
    calls = []

    def planner(summary, prompt, directory, config, preview_paths, phase="normal", context=None, lecterns=None):
        directory.mkdir()
        calls.append({"phase": phase, "lecterns": lecterns, "context": context})
        operation = ({"op": "fill", "min": [5, 1, 6], "max": [5, 1, 6], "block": "minecraft:stone"}
                     if phase == "draft" else {"op": "fill", "min": [0, 0, 0], "max": [0, 0, 0], "block": "minecraft:stone"})
        return SimpleNamespace(plan={"version": 1, "summary": phase, "operations": [operation]},
                               usage={}, elapsed_seconds=0.1, model="fixture")

    plan_volume(source, "Furnish the rooms", tmp_path / "call", tmp_path / "previews", agent_config_for({}, effort),
                effort=effort, planner=planner, lecterns=ROOMS,
                viewpoint_context={"selection_origin": ORIGIN})
    assert [call["phase"] for call in calls] == phases
    assert all(call["lecterns"] == ROOMS for call in calls)
    if effort == "high":
        check = calls[1]["context"]["lectern_check"]
        assert [item["unchanged_in_candidate"] for item in check] == [True, False]
        assert "lectern_check" not in calls[0]["context"]
    # A mismatched world position is rejected before any planner call.
    calls.clear()
    with pytest.raises(ValueError, match="does not match"):
        plan_volume(source, "Furnish", tmp_path / "bad", tmp_path / "previews2", agent_config_for({}, effort),
                    effort=effort, planner=planner, lecterns=ROOMS,
                    viewpoint_context={"selection_origin": [0, 0, 0]})
    assert calls == []


def test_pipeline_without_lecterns_keeps_old_planner_signature(tmp_path):
    def planner(summary, prompt, directory, config, preview_paths):
        directory.mkdir()
        return SimpleNamespace(plan={"version": 1, "summary": "ok", "operations": [
            {"op": "fill", "min": [0, 0, 0], "max": [0, 0, 0], "block": "minecraft:stone"}]},
            usage={}, elapsed_seconds=0.1, model="fixture")
    result = plan_volume(volume(), "Mark", tmp_path / "call", tmp_path / "previews", agent_config_for({}),
                         planner=planner, lecterns=[])
    assert result.plan["summary"] == "ok"
    assert result.edited.non_air_mask().sum() == 1


def test_preview_tool_marks_lectern_positions(tmp_path):
    from worldprompt_core.planner_tools import PlannerTools
    from worldprompt_core.spatial_tools import write_snapshot
    source = volume([2, 1, 2])
    write_snapshot(source, tmp_path)
    tools = PlannerTools({"volume": {"dimensions": DIMENSIONS}, "limits": {"max_operations": 8},
                          "lectern_instructions": [lectern([2, 1, 2]), {"position": [99, 0, 0], "text": "ignored"}]},
                         tmp_path)
    plan = {"version": 1, "summary": "over", "operations": [
        {"op": "fill", "min": [0, 1, 0], "max": [3, 1, 3], "block": "minecraft:stone"}]}
    result = tools.call("preview_plan", {"plan": plan})
    assert result["lectern_check"] == [{"position": [2, 1, 2], "source_state": LECTERN,
                                        "candidate_state": "minecraft:stone", "unchanged_in_candidate": False}]
    plain = PlannerTools({"volume": {"dimensions": DIMENSIONS}, "limits": {"max_operations": 8}}, tmp_path)
    assert "lectern_check" not in plain.call("preview_plan", {"plan": plan})


@pytest.fixture
def real_supervisor(tmp_path):
    fake = tmp_path / "fake_cli.py"
    fake.write_text(FAKE_CLI)
    supervisor = JobSupervisor({"jobs_dir": str(tmp_path / "jobs"), "world_concurrency": 1,
                                "codex_command": [sys.executable, str(fake), "ok"],
                                "timeout_seconds": 30, "high_timeout_seconds": 30})
    yield supervisor
    supervisor.close()


def lectern_submission(**updates):
    return submission(viewpoint=viewpoint(), source=source_payload([2, 1, 2], [5, 1, 6]),
                      lecterns=copy.deepcopy(ROOMS), **updates)


@pytest.mark.parametrize("effort,phases", [("normal", ["normal"]), ("high", ["draft", "review"])])
def test_supervisor_round_trip_reaches_every_model_pass(real_supervisor, tmp_path, effort, phases):
    job = real_supervisor.submit(lectern_submission(effort=effort))
    assert job["lecterns"] == ROOMS
    ready = wait_status(real_supervisor, job["id"], {"ready", "failed"}, timeout=60)
    assert ready["status"] == "ready", ready
    directory = tmp_path / "jobs" / "world" / job["id"]
    assert json.loads((directory / "request.json").read_text())["lecterns"] == ROOMS
    assert json.loads((directory / "job.json").read_text())["lecterns"] == ROOMS
    for phase in phases:
        call = directory / "planning" / phase if effort == "high" else directory / "planning"
        request = json.loads((call / "request.json").read_text())
        assert request["lectern_instructions"] == ROOMS
        assert "Lectern instructions" in (call / "instructions.md").read_text()
        if phase == "review":
            assert [item["position"] for item in request["context"]["lectern_check"]] == [[2, 1, 2], [5, 1, 6]]
    assert real_supervisor.result(job["id"])["operation_count"] == 3


def test_supervisor_accepts_submissions_without_lecterns(real_supervisor):
    job = real_supervisor.submit(submission(viewpoint=viewpoint(), source=source_payload()))
    assert job["lecterns"] == []
    assert wait_status(real_supervisor, job["id"], {"ready", "failed"}, timeout=60)["status"] == "ready"


@pytest.mark.parametrize("change", [
    lambda body: body["lecterns"][0].update(position=[3, 1, 2]),
    lambda body: body["lecterns"][0].update(world_position=[0, 0, 0]),
    lambda body: body["lecterns"][0].update(text="bell\x07"),
    lambda body: body.update(lecterns={"position": [2, 1, 2]}),
])
def test_invalid_lecterns_never_occupy_the_owner(real_supervisor, tmp_path, change):
    body = lectern_submission()
    change(body)
    with pytest.raises(ValueError):
        real_supervisor.submit(body)
    assert real_supervisor.list_jobs() == []
    assert not list((tmp_path / "jobs" / "world").glob("*/request.json"))


def test_world_http_rejects_invalid_lecterns_with_400(tmp_path):
    import http.client
    import threading
    from http.server import ThreadingHTTPServer
    from worldprompt_core.world_server import make_world_handler
    supervisor = JobSupervisor({"jobs_dir": str(tmp_path / "jobs")}, start=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_world_handler(supervisor, "test-token"))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        body = lectern_submission()
        body["lecterns"][1]["text"] = "z" * (MAX_BOOK_CHARACTERS + 1)
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
        connection.request("POST", "/api/world/jobs", body=json.dumps(body).encode(), headers={
            "Content-Type": "application/json", "Authorization": "Bearer test-token",
            "X-WorldPrompt-Mod-Version": __version__})
        response = connection.getresponse()
        assert response.status == 400
        assert "exceeds 4000 characters" in json.loads(response.read())["error"]
        connection.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        supervisor.close()
