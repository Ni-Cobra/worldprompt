"""World supervisor integration tests with real isolated worker processes.

The fake planner protocol is deterministic and never calls an account or model.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import time
import uuid

import pytest

from worldprompt_core.world_jobs import JobSupervisor, PlayerJobLimitError
from process_checks import process_is_running


OWNER_A = "11111111-1111-4111-8111-111111111111"
OWNER_B = "22222222-2222-4222-8222-222222222222"
OWNER_C = "33333333-3333-4333-8333-333333333333"


def submission(owner=OWNER_A, prompt="marker", **updates):
    value = {
        "request_id": str(uuid.uuid4()),
        "owner_id": owner,
        "owner_name": {OWNER_A: "Alice", OWNER_B: "Bob", OWNER_C: "Carol"}[owner],
        "prompt": prompt,
        "effort": "normal",
        "source": {"dimensions": [3, 2, 2],
                   "palette": ["minecraft:air", "minecraft:stone"],
                   "blocks": [0] * 12, "data_version": 4901},
    }
    value.update(updates)
    return value


def isolated_worker(config, request_path, connection):
    """Importable spawn target with distinct crash, block and successful paths."""
    request_path = Path(request_path)
    request = json.loads(request_path.read_text())
    directory = request_path.parent
    (directory / "worker-pid.txt").write_text(str(os.getpid()))
    (directory / "worker-observed.json").write_text(json.dumps(request))
    connection.send({"kind": "progress", "values": {
        "status": "planning", "message": "Deterministic fixture planning"}})
    if request["prompt"] == "crash":
        os._exit(17)
    if request["prompt"] == "review-failure":
        connection.send({"kind": "progress", "values": {
            "status": "reviewing", "message": "Deterministic fixture reviewing"}})
        raise RuntimeError("Review failed; no final plan exists")
    if request["prompt"] == "blocked":
        deadline = time.monotonic() + 20
        while not (directory / "release").exists() and time.monotonic() < deadline:
            time.sleep(.02)

    plan = {"version": 1, "summary": request["prompt"], "operations": [
        {"op": "fill", "min": [0, 0, 0], "max": [1, 0, 1],
         "block": "minecraft:stone"},
        {"op": "fill", "min": [1, 0, 0], "max": [1, 0, 0],
         "block": "minecraft:air"}]}
    if request["effort"] == "high":
        connection.send({"kind": "progress", "values": {
            "status": "checking", "draft_plan_summary": "draft of " + request["prompt"]}})
    from worldprompt_core.replay import compile_replay
    from worldprompt_core.world_worker import snapshot_volume
    manifest = compile_replay(snapshot_volume(request["source"]), plan,
                              directory / "replay").manifest
    connection.send({"kind": "progress", "values": {
        "status": "validating", "message": "Deterministic fixture compiling", "summary": plan["summary"]}})
    connection.send({"kind": "ready", "manifest": manifest})
    connection.close()


@pytest.fixture
def supervisor_factory(tmp_path):
    supervisors = []

    def build(**updates):
        config = {"jobs_dir": str(tmp_path / "jobs"), "job_limits": True,
                  "world_concurrency": 2, "world_lease_seconds": 120}
        config.update(updates)
        supervisor = JobSupervisor(config, worker_target=isolated_worker)
        supervisors.append(supervisor)
        return supervisor

    yield build
    for supervisor in reversed(supervisors):
        supervisor.close()


def wait_status(supervisor, job_id, statuses, timeout=10):
    if isinstance(statuses, str):
        statuses = {statuses}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = supervisor.get_job(job_id)
        if job["status"] in statuses:
            return job
        time.sleep(.02)
    pytest.fail(f"Job {job_id} did not reach {statuses}: {job}")


def job_directory(tmp_path, job):
    return tmp_path / "jobs" / "world" / job["id"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX file-mode regression")
def test_new_job_artifacts_are_private_even_with_permissive_caller_umask(tmp_path):
    previous = os.umask(0o022)
    try:
        supervisor = JobSupervisor({"jobs_dir": str(tmp_path / "jobs")}, start=False)
        try:
            job = supervisor.submit(submission())
            root = tmp_path / "jobs/world"
            directory = root / job["id"]
            assert root.stat().st_mode & 0o777 == 0o700
            assert directory.stat().st_mode & 0o777 == 0o700
            for name in ("request.json", "job.json"):
                assert (directory / name).stat().st_mode & 0o777 == 0o600
        finally:
            supervisor.close()
    finally:
        os.umask(previous)


def test_concurrent_workers_are_isolated_queue_is_bounded_and_ready_keeps_owner(
        supervisor_factory, tmp_path):
    supervisor = supervisor_factory()
    source_a = submission(prompt="blocked")
    before = copy.deepcopy(source_a)
    a = supervisor.submit(source_a)
    b = supervisor.submit(submission(OWNER_B, "blocked", effort="high"))
    c = supervisor.submit(submission(OWNER_C, "marker"))
    source_a["source"]["blocks"][0] = 1
    source_a["prompt"] = "caller mutated source"
    wait_status(supervisor, a["id"], "planning")
    wait_status(supervisor, b["id"], "planning")
    assert supervisor.get_job(c["id"])["status"] == "queued"
    assert supervisor.get_job(c["id"])["queue_position"] == 1
    assert {entry["owner_id"] for entry in supervisor.list_jobs()} == {
        OWNER_A, OWNER_B, OWNER_C}
    assert supervisor.get_job(b["id"])["effort"] == "high"

    a_dir, b_dir = job_directory(tmp_path, a), job_directory(tmp_path, b)
    observed = json.loads((a_dir / "worker-observed.json").read_text())
    assert observed["source"] == before["source"]
    assert observed["prompt"] == before["prompt"]
    assert (a_dir / "worker-pid.txt").read_text() != (b_dir / "worker-pid.txt").read_text()
    assert int((a_dir / "worker-pid.txt").read_text()) != os.getpid()

    (a_dir / "release").touch()
    wait_status(supervisor, a["id"], "ready")
    wait_status(supervisor, c["id"], "ready")
    assert supervisor.get_job(b["id"])["status"] == "planning"
    supervisor.application(a["id"], "reconstructing", operation=0)
    assert supervisor.get_job(a["id"])["status"] == "reconstructing"
    supervisor.application(a["id"], "reconstructing", operation=2)
    supervisor.application(a["id"], "completed", operation=2)
    assert supervisor.get_job(a["id"])["status"] == "completed"
    assert a["id"] not in {entry["id"] for entry in supervisor.list_jobs()}
    next_a = supervisor.submit(submission(OWNER_A, "new job"))
    wait_status(supervisor, next_a["id"], "ready")


def test_result_contains_all_original_operations_and_overwrites(supervisor_factory):
    supervisor = supervisor_factory()
    job = supervisor.submit(submission())
    wait_status(supervisor, job["id"], "ready")
    result = supervisor.result(job["id"])
    assert result["operation_count"] == 2
    assert result["plan"]["operations"][0]["block"] == "minecraft:stone"
    assert result["plan"]["operations"][1]["block"] == "minecraft:air"
    import base64
    import struct
    def runs(index):
        return list(struct.iter_unpack("<III", base64.b64decode(result["operations"][index]["runs_base64"])))
    assert result["result_version"] == 2 and result["run_encoding"] == "base64-u32le"
    assert runs(0) == [(0, 2, 1), (3, 2, 1)]
    assert runs(1) == [(1, 1, 0)]


def test_double_pass_result_carries_the_draft_summary(supervisor_factory):
    supervisor = supervisor_factory()
    double = supervisor.submit(submission(prompt="tower", effort="high"))
    single = supervisor.submit(submission(OWNER_B, "hut"))
    wait_status(supervisor, double["id"], "ready")
    wait_status(supervisor, single["id"], "ready")
    result = supervisor.result(double["id"])
    assert (result["draft_plan_summary"], result["plan"]["summary"]) == ("draft of tower", "tower")
    assert "draft_plan_summary" not in supervisor.result(single["id"]), "Single pass has no draft"
    assert all("draft_plan_summary" not in job for job in supervisor.list_jobs(brief=True))


@pytest.mark.parametrize("prompt", ["crash", "review-failure"])
def test_failed_worker_does_not_publish_or_poison_another_player(supervisor_factory, prompt):
    supervisor = supervisor_factory()
    broken = supervisor.submit(submission(prompt=prompt, effort="high"))
    healthy = supervisor.submit(submission(OWNER_B, "healthy plan"))
    failed = wait_status(supervisor, broken["id"], "failed")
    assert failed["message"]
    wait_status(supervisor, healthy["id"], "ready")
    assert supervisor.result(healthy["id"])["plan"]["summary"] == "healthy plan"
    assert supervisor.get_job(healthy["id"])["summary"] == "healthy plan", "The GUI shows the final plan's summary"
    with pytest.raises(ValueError):
        supervisor.result(broken["id"])
    replacement = supervisor.submit(submission(OWNER_A, "recovered"))
    wait_status(supervisor, replacement["id"], "ready")


def test_cancel_running_job_reaps_worker_before_releasing_owner(supervisor_factory, tmp_path):
    supervisor = supervisor_factory(world_concurrency=1)
    job = supervisor.submit(submission(prompt="blocked"))
    wait_status(supervisor, job["id"], "planning")
    pid = int((job_directory(tmp_path, job) / "worker-pid.txt").read_text())
    supervisor.cancel(job["id"], "Player disconnected")
    wait_status(supervisor, job["id"], "failed")
    assert not process_is_running(pid)
    following = supervisor.submit(submission(prompt="following"))
    wait_status(supervisor, following["id"], "ready")


def test_cancel_queued_job_never_starts_worker(supervisor_factory, tmp_path):
    supervisor = supervisor_factory(world_concurrency=1)
    first = supervisor.submit(submission(prompt="blocked"))
    wait_status(supervisor, first["id"], "planning")
    queued = supervisor.submit(submission(OWNER_B, "queued"))
    assert supervisor.get_job(queued["id"])["status"] == "queued"
    supervisor.cancel(queued["id"], "Player disconnected")
    wait_status(supervisor, queued["id"], "failed")
    assert not (job_directory(tmp_path, queued) / "worker-pid.txt").exists()
    following = supervisor.submit(submission(OWNER_B, "replacement"))
    assert following["id"] != queued["id"]


def test_idempotent_retry_does_not_duplicate_worker_or_allow_changed_owner(supervisor_factory):
    supervisor = supervisor_factory()
    request = submission(prompt="blocked")
    original = supervisor.submit(request)
    retry = supervisor.submit(copy.deepcopy(request))
    assert retry["id"] == original["id"]
    assert len(supervisor.list_jobs()) == 1
    changed_owner = copy.deepcopy(request)
    changed_owner["owner_id"] = OWNER_B
    changed_owner["owner_name"] = "Bob"
    with pytest.raises(ValueError):
        supervisor.submit(changed_owner)


def test_application_requires_ready_and_monotonic_complete_progress(supervisor_factory):
    supervisor = supervisor_factory()
    job = supervisor.submit(submission(prompt="blocked"))
    wait_status(supervisor, job["id"], "planning")
    with pytest.raises(ValueError):
        supervisor.application(job["id"], "reconstructing")
    supervisor.cancel(job["id"])
    wait_status(supervisor, job["id"], "failed")
    job = supervisor.submit(submission())
    wait_status(supervisor, job["id"], "ready")
    with pytest.raises(ValueError):
        supervisor.application(job["id"], "completed", operation=2)
    supervisor.application(job["id"], "reconstructing", operation=1)
    with pytest.raises(ValueError):
        supervisor.application(job["id"], "reconstructing", operation=0)
    with pytest.raises(ValueError):
        supervisor.application(job["id"], "completed", operation=1)
    supervisor.application(job["id"], "reconstructing", operation=2)
    supervisor.application(job["id"], "completed", operation=2)


def test_partial_application_failure_preserves_snapshot_and_replay(
        supervisor_factory, tmp_path):
    supervisor = supervisor_factory()
    request = submission()
    job = supervisor.submit(request)
    wait_status(supervisor, job["id"], "ready")
    supervisor.application(job["id"], "reconstructing", operation=1)
    supervisor.application(job["id"], "failed", operation=1, message="Adapter disconnected")
    assert supervisor.get_job(job["id"])["status"] == "failed"
    directory = job_directory(tmp_path, job)
    assert (directory / "replay" / "plan.json").exists()
    assert any(json.loads(path.read_text()).get("source") == request["source"]
               for path in directory.glob("*.json"))


@pytest.mark.parametrize("updates", [
    {"owner_id": "display-name"},
    {"effort": "HIGH"},
    {"effort": True},
    {"prompt": ""},
    {"source": {"dimensions": [3, 2, 2], "palette": ["minecraft:air"],
                "blocks": [0] * 11}},
    {"source": {"dimensions": [3, 2, 2], "palette": ["minecraft:air"],
                "blocks": [1] * 12}},
    {"source": {"dimensions": [1, 1, 4_000_001], "palette": ["minecraft:air"],
                "blocks": []}},
])
def test_invalid_submission_never_occupies_owner(supervisor_factory, updates):
    supervisor = supervisor_factory()
    with pytest.raises(ValueError):
        supervisor.submit(submission(**updates))
    assert supervisor.list_jobs() == []


def test_expired_adapter_lease_releases_queue_without_jobs_list_heartbeat(tmp_path, monkeypatch):
    from worldprompt_core import world_jobs
    clock = [1000.0]
    monkeypatch.setattr(world_jobs.time, "time", lambda: clock[0])
    supervisor = JobSupervisor(
        {"jobs_dir": str(tmp_path / "jobs"), "world_lease_seconds": 10},
        worker_target=isolated_worker, start=False)
    try:
        job = supervisor.submit(submission(prompt="blocked"))
        clock[0] += 8
        assert supervisor.list_jobs()[0]["id"] == job["id"]
        clock[0] += 3
        supervisor.tick()
        assert supervisor.get_job(job["id"])["status"] == "failed"
        assert not (job_directory(tmp_path, job) / "worker-pid.txt").exists()
        assert supervisor.submit(submission())["id"] != job["id"]
    finally:
        supervisor.close()


def test_restart_marks_unacknowledged_world_application_failed_keeps_artifacts(
        supervisor_factory, tmp_path):
    supervisor = supervisor_factory()
    job = supervisor.submit(submission())
    wait_status(supervisor, job["id"], "ready")
    supervisor.application(job["id"], "reconstructing", operation=1)
    supervisor.close()
    path = job_directory(tmp_path, job) / "job.json"
    persisted = json.loads(path.read_text())
    persisted["status"] = "reconstructing"
    path.write_text(json.dumps(persisted))
    restarted = supervisor_factory()
    recovered = restarted.get_job(job["id"])
    assert recovered["status"] == "failed"
    assert "restart" in recovered["message"].lower()
    assert (path.parent / "replay" / "plan.json").exists()
    assert "/wp undo" in recovered["message"]
    assert restarted.list_jobs() == []
    new = restarted.submit(submission())
    wait_status(restarted, new["id"], "ready")


def test_second_supervisor_cannot_adopt_a_running_directory(supervisor_factory):
    supervisor = supervisor_factory()
    job = supervisor.submit(submission(prompt="blocked"))
    wait_status(supervisor, job["id"], "planning")
    with pytest.raises(ValueError, match="supervisor"):
        supervisor_factory()
    assert supervisor.get_job(job["id"])["status"] == "planning"


@pytest.mark.parametrize("limits, expected", [({"job_limits": False, "world_max_jobs_per_player": 1}, 8),
                                              ({"world_max_jobs_per_player": 1}, 1), ({"world_max_jobs_per_player": 3}, 3)])
def test_concurrent_submissions_from_same_player_respect_the_per_player_limit(
        supervisor_factory, limits, expected):
    import concurrent.futures
    supervisor = supervisor_factory(**limits)
    def attempt():
        try:
            return supervisor.submit(submission(prompt="blocked"))
        except PlayerJobLimitError:
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: attempt(), range(8)))
    accepted = [job for job in results if job is not None]
    assert len(accepted) == expected
    assert len(supervisor.list_jobs()) == expected
    # Admission order, not thread order, decides which jobs the two workers take.
    wait_status(supervisor, supervisor.list_jobs()[0]["id"], "planning")


def test_one_player_runs_several_jobs_with_independent_queue_cancel_and_results(
        supervisor_factory, tmp_path):
    supervisor = supervisor_factory(world_concurrency=2)
    assert supervisor.max_jobs_per_player == 4
    first = supervisor.submit(submission(prompt="blocked"))
    second = supervisor.submit(submission(prompt="blocked"))
    third = supervisor.submit(submission(prompt="third plan"))
    other = supervisor.submit(submission(OWNER_B, "other player"))
    wait_status(supervisor, first["id"], "planning")
    wait_status(supervisor, second["id"], "planning")
    assert supervisor.get_job(third["id"])["queue_position"] == 1
    assert supervisor.get_job(other["id"])["queue_position"] == 2
    listed = supervisor.list_jobs()
    assert [entry["id"] for entry in listed if entry["owner_id"] == OWNER_A] == [
        first["id"], second["id"], third["id"]]
    assert supervisor.info()["active_jobs"] == 2 and supervisor.info()["queued_jobs"] == 2
    # Cancelling one of the owner's jobs stops only that worker; the next queued job starts.
    pid = int((job_directory(tmp_path, first) / "worker-pid.txt").read_text())
    supervisor.cancel(first["id"], "Player cancelled one job")
    wait_status(supervisor, first["id"], "failed")
    assert not process_is_running(pid)
    assert supervisor.get_job(second["id"])["status"] == "planning"
    wait_status(supervisor, third["id"], "ready")
    assert supervisor.result(third["id"])["plan"]["summary"] == "third plan"
    (job_directory(tmp_path, second) / "release").touch()
    wait_status(supervisor, second["id"], "ready")
    assert supervisor.result(second["id"])["plan"]["summary"] == "blocked"
    wait_status(supervisor, other["id"], "ready")
    for job in (second, third):
        supervisor.application(job["id"], "reconstructing", operation=2)
        supervisor.application(job["id"], "completed", operation=2)
    assert [entry["id"] for entry in supervisor.list_jobs()] == [other["id"]]


def test_per_player_limit_counts_unfinished_and_stopping_jobs(supervisor_factory):
    supervisor = supervisor_factory(world_concurrency=1, world_max_jobs_per_player=2)
    running = supervisor.submit(submission(prompt="blocked"))
    queued = supervisor.submit(submission(prompt="queued"))
    wait_status(supervisor, running["id"], "planning")
    with pytest.raises(PlayerJobLimitError, match="at most 2 active WorldPrompt jobs per player") as rejected:
        supervisor.submit(submission(prompt="third"))
    assert rejected.value.code == "player_job_limit" and rejected.value.limit == 2
    assert len(supervisor.list_jobs()) == 2
    # Other players are unaffected by one owner's limit.
    other = supervisor.submit(submission(OWNER_B, "other"))
    assert other["status"] == "queued"
    # A job that is still stopping keeps its slot until its worker exits.
    with supervisor.lock:
        supervisor.cancel(running["id"], "Stopping")
        assert supervisor.jobs[running["id"]]["status"] == "cancelling"
        with pytest.raises(PlayerJobLimitError):
            supervisor.submit(submission(prompt="while stopping"))
    wait_status(supervisor, running["id"], "failed")
    wait_status(supervisor, queued["id"], "ready")
    # Ready and reconstructing jobs still count until the mod acknowledges completion.
    replacement = supervisor.submit(submission(prompt="replacement"))
    with pytest.raises(PlayerJobLimitError):
        supervisor.submit(submission(prompt="ready still counts"))
    supervisor.application(queued["id"], "reconstructing", operation=0)
    with pytest.raises(PlayerJobLimitError):
        supervisor.submit(submission(prompt="reconstructing still counts"))
    supervisor.application(queued["id"], "reconstructing", operation=2)
    supervisor.application(queued["id"], "completed", operation=2)
    assert supervisor.submit(submission(prompt="slot released"))["owner_id"] == OWNER_A
    # Lowering the limit live never cancels accepted work; raising it admits jobs again.
    supervisor.max_jobs_per_player = 1
    with pytest.raises(PlayerJobLimitError, match="at most 1 active WorldPrompt job per player"):
        supervisor.submit(submission(prompt="over a lowered limit"))
    supervisor.max_jobs_per_player = 3
    supervisor.submit(submission(prompt="raised again"))
    owned = [entry["id"] for entry in supervisor.list_jobs() if entry["owner_id"] == OWNER_A]
    assert len(owned) == 3 and replacement["id"] in owned


def test_idempotent_retry_is_accepted_at_the_per_player_limit(supervisor_factory):
    supervisor = supervisor_factory(world_max_jobs_per_player=1)
    request = submission(prompt="blocked")
    original = supervisor.submit(request)
    assert supervisor.submit(copy.deepcopy(request))["id"] == original["id"]
    assert len(supervisor.list_jobs()) == 1


@pytest.mark.parametrize("value", [-1, 0, True, 1.5, "2"])
def test_supervisor_rejects_invalid_per_player_limit(tmp_path, value):
    with pytest.raises(ValueError, match="world_max_jobs_per_player"):
        JobSupervisor({"jobs_dir": str(tmp_path / "jobs"), "job_limits": True, "world_max_jobs_per_player": value},
                      start=False)


def test_supervisor_ignores_stored_limits_while_job_limits_are_off(tmp_path):
    supervisor = JobSupervisor({"jobs_dir": str(tmp_path / "jobs"), "world_concurrency": 1, "world_max_pending": 1,
                                "world_max_jobs_per_player": 1, "timeout_seconds": 10}, start=False)
    try:
        assert (supervisor.concurrency, supervisor.max_pending, supervisor.max_jobs_per_player) == (
            1_000_000, 1_000_000, 1_000_000)
        info = supervisor.info()
        assert info["job_limits"] is False and info["timeout_seconds"] == info["high_timeout_seconds"] == 365 * 86_400
    finally:
        supervisor.close()


def test_queue_capacity_bounds_unfinished_jobs(supervisor_factory):
    supervisor = supervisor_factory(world_concurrency=1, world_max_pending=2)
    a = supervisor.submit(submission(prompt="blocked"))
    b = supervisor.submit(submission(OWNER_B, "blocked"))
    with pytest.raises(ValueError, match="full"):
        supervisor.submit(submission(OWNER_C))
    supervisor.cancel(b["id"])
    wait_status(supervisor, b["id"], "failed")
    assert supervisor.submit(submission(OWNER_C))["status"] == "queued"
