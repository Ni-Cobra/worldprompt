"""Supervisor resilience: retained artifacts, PID safety, pinned reads, atomic and private writes."""
import json
import os
from pathlib import Path
import stat
import threading
import time

import pytest

from worldprompt_core import config as config_module
from worldprompt_core import world_jobs
from worldprompt_core.world_jobs import JobNotFound, JobSupervisor, process_start_time
from worldprompt_core.world_server import adapter_token, token_permission_warning
from test_world_jobs import (OWNER_A, OWNER_B, isolated_worker, job_directory, submission,
                             supervisor_factory, wait_status)

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics")
proc_only = pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="needs Linux /proc")


def complete(supervisor, job_id):
    wait_status(supervisor, job_id, "ready")
    supervisor.application(job_id, "reconstructing", operation=2)
    return supervisor.application(job_id, "completed", operation=2)


def test_finished_jobs_keep_their_capture_and_replay(supervisor_factory, tmp_path):
    supervisor = supervisor_factory()
    job = supervisor.submit(submission())
    directory = job_directory(tmp_path, job)
    wait_status(supervisor, job["id"], "ready")
    supervisor.application(job["id"], "reconstructing", operation=2)
    done = supervisor.application(job["id"], "completed", operation=2)
    assert done["status"] == "completed"
    # The GUI keeps captures and plans for inspection until the user clears history.
    assert (directory / "request.json").is_file() and (directory / "replay" / "manifest.json").is_file()
    assert supervisor.result(job["id"])["operation_count"] == 2
    supervisor.close()
    restarted = supervisor_factory()
    assert restarted.result(job["id"])["operation_count"] == 2
    assert (directory / "request.json").is_file()


@pytest.mark.parametrize("prompt", ["crash", "review-failure"])
def test_failed_job_keeps_its_capture(supervisor_factory, tmp_path, prompt):
    supervisor = supervisor_factory()
    job = supervisor.submit(submission(prompt=prompt))
    wait_status(supervisor, job["id"], "failed")
    directory = job_directory(tmp_path, job)
    assert (directory / "job.json").is_file() and (directory / "request.json").is_file()


def test_replay_is_read_outside_the_lock_and_removal_waits_for_the_reader(
        supervisor_factory, tmp_path, monkeypatch):
    from worldprompt_core import replay
    supervisor = supervisor_factory()
    job = supervisor.submit(submission())
    wait_status(supervisor, job["id"], "ready")
    supervisor.application(job["id"], "reconstructing", operation=1)
    real_read = replay.read_complete_result
    reading, proceed = threading.Event(), threading.Event()

    def slow_read(destination):
        reading.set()
        assert proceed.wait(10)
        return real_read(destination)

    monkeypatch.setattr(replay, "read_complete_result", slow_read)
    outcome = {}
    reader = threading.Thread(target=lambda: outcome.update(result=supervisor.result(job["id"])))
    reader.start()
    try:
        assert reading.wait(10)
        # The lock is free while the payload is being read.
        assert supervisor.get_job(job["id"])["status"] == "reconstructing"
        supervisor.application(job["id"], "failed", operation=1, message="Adapter disconnected")
        directory = job_directory(tmp_path, job)
        # Terminal now, but the pinned reader still blocks removal.
        assert "downloading" in supervisor.cleanup_blocked_reason(supervisor.jobs[job["id"]])
        with pytest.raises(ValueError, match="downloading"):
            supervisor.remove_jobs([job["id"]])
    finally:
        proceed.set()
        reader.join(10)
    assert outcome["result"]["operation_count"] == 2
    assert (directory / "replay" / "manifest.json").is_file()
    assert supervisor.cleanup_blocked_reason(supervisor.jobs[job["id"]]) is None
    assert supervisor.remove_jobs([job["id"]]) == 1


def test_submit_writes_the_capture_outside_the_lock(supervisor_factory, monkeypatch):
    supervisor = supervisor_factory()
    writing, proceed = threading.Event(), threading.Event()
    real_fdopen = os.fdopen

    def slow_fdopen(*args, **kwargs):
        writing.set()
        assert proceed.wait(10)
        return real_fdopen(*args, **kwargs)

    monkeypatch.setattr(world_jobs.os, "fdopen", slow_fdopen)
    outcome = {}
    submitter = threading.Thread(target=lambda: outcome.update(job=supervisor.submit(submission())))
    submitter.start()
    try:
        assert writing.wait(10)
        started = time.monotonic()
        assert supervisor.info()["queued_jobs"] == 0
        assert time.monotonic() - started < 2
    finally:
        proceed.set()
        submitter.join(10)
    assert outcome["job"]["status"] in {"queued", "preparing"}
    assert not list(supervisor.directory.glob(".submit-*"))


def test_unknown_and_malformed_ids_raise_job_not_found(supervisor_factory):
    supervisor = supervisor_factory()
    for job_id in ("not-a-uuid", "00000000-0000-4000-8000-000000000000", None):
        with pytest.raises(JobNotFound, match="Job not found."):
            supervisor.get_job(job_id)
    assert str(JobNotFound("Job not found.")) == "Job not found."


def test_watchdog_survives_a_failing_pass_and_logs_it(tmp_path, caplog):
    supervisor = JobSupervisor({"jobs_dir": str(tmp_path)}, worker_target=isolated_worker, start=False)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) <= 2:
            raise RuntimeError("Fixture supervisor fault")

    supervisor.tick = flaky
    supervisor.thread = threading.Thread(target=supervisor._watch, daemon=True)
    with caplog.at_level("ERROR", logger="worldprompt.supervisor"):
        supervisor.thread.start()
        deadline = time.monotonic() + 5
        while len(calls) < 4 and time.monotonic() < deadline:
            time.sleep(.02)
    try:
        assert len(calls) >= 4 and supervisor.thread.is_alive()
        faults = [r for r in caplog.records if "Fixture supervisor fault" in (r.exc_text or "")]
        assert len(faults) == 1  # an identical repeating fault is not logged at 10 Hz
    finally:
        supervisor.tick = lambda: None
        supervisor.close()


def test_pipe_failure_fails_only_that_job_and_keeps_the_queue_alive(tmp_path):
    supervisor = JobSupervisor({"jobs_dir": str(tmp_path)}, worker_target=isolated_worker, start=False)

    class BrokenContext:
        def Pipe(self, duplex=False):
            raise OSError("Fixture: too many open files")

    try:
        job = supervisor.submit(submission())
        real_context, supervisor.context = supervisor.context, BrokenContext()
        supervisor.tick()
        failed = supervisor.get_job(job["id"])
        assert failed["status"] == "failed" and "too many open files" in failed["message"]
        supervisor.context = real_context
        later = supervisor.submit(submission(OWNER_B))
        supervisor.tick()
        wait_status_manually(supervisor, later["id"])
    finally:
        supervisor.close()


def wait_status_manually(supervisor, job_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        supervisor.tick()
        if supervisor.get_job(job_id)["status"] == "ready":
            return
        time.sleep(.02)
    pytest.fail("Job never became ready")


class ScriptedConnection:
    def __init__(self, *events):
        self.events = list(events)

    def poll(self):
        return bool(self.events)

    def recv(self):
        return self.events.pop(0)


@posix_only
def test_drain_records_the_provider_start_time_and_clears_it_with_the_pid(tmp_path, monkeypatch):
    supervisor = JobSupervisor({"jobs_dir": str(tmp_path)}, start=False)
    monkeypatch.setattr(world_jobs, "process_start_time", lambda pid: 4242 if pid == 777 else None)
    try:
        job = supervisor.submit(submission())
        worker = {"connection": ScriptedConnection({"kind": "process", "pid": 777}), "process": None}
        supervisor._drain_worker(supervisor.jobs[job["id"]], worker)
        assert (worker["cli_pid"], worker["cli_start"]) == (777, 4242)
        worker["connection"] = ScriptedConnection({"kind": "process_finished", "pid": 777})
        supervisor._drain_worker(supervisor.jobs[job["id"]], worker)
        assert "cli_pid" not in worker and "cli_start" not in worker
    finally:
        supervisor.close()


@posix_only
@proc_only
@pytest.mark.parametrize("current, killed", [(4242, True), (999, False), (None, True)])
def test_provider_group_is_signalled_only_for_the_recorded_process(monkeypatch, current, killed):
    signalled = []
    monkeypatch.setattr(world_jobs, "process_start_time", lambda pid: current)
    monkeypatch.setattr(world_jobs.os, "killpg", lambda pid, signum: signalled.append((pid, signum)))
    JobSupervisor._stop_provider_group({"cli_pid": 31337, "cli_start": 4242}, 15)
    assert signalled == ([(31337, 15)] if killed else [])


@posix_only
@proc_only
def test_unrecorded_start_time_never_matches_a_live_process(monkeypatch):
    # The CLI was already gone when its PID arrived, so a live process now is a stranger.
    signalled = []
    monkeypatch.setattr(world_jobs, "process_start_time", lambda pid: 12345)
    monkeypatch.setattr(world_jobs.os, "killpg", lambda *args: signalled.append(args))
    JobSupervisor._stop_provider_group({"cli_pid": 31337, "cli_start": None}, 9)
    assert signalled == []


@posix_only
@pytest.mark.parametrize("error", [PermissionError(1, "denied"), ProcessLookupError(3, "gone"), OSError(22, "bad")])
def test_stopping_a_provider_group_never_raises(monkeypatch, error):
    def fail(pid, signum):
        raise error

    monkeypatch.setattr(world_jobs.os, "killpg", fail)
    monkeypatch.setattr(world_jobs, "process_start_time", lambda pid: 7)
    JobSupervisor._stop_provider_group({"cli_pid": 31337, "cli_start": 7}, 15)


@proc_only
def test_process_start_time_identifies_one_process():
    import subprocess
    import sys
    own = process_start_time(os.getpid())
    assert isinstance(own, int) and own == process_start_time(os.getpid())
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        started = process_start_time(child.pid)
        assert isinstance(started, int) and started >= own
    finally:
        child.kill()
        child.wait()
    assert process_start_time(child.pid) is None


@posix_only
def test_supervisor_leaves_the_process_umask_alone_and_writes_privately(tmp_path):
    previous = os.umask(0o022)
    try:
        supervisor = JobSupervisor({"jobs_dir": str(tmp_path / "jobs")}, start=False)
        try:
            assert os.umask(0o022) == 0o022
            job = supervisor.submit(submission())
            root = tmp_path / "jobs/world"
            assert (root / "supervisor.lock").stat().st_mode & 0o777 == 0o600
            assert (root / job["id"]).stat().st_mode & 0o777 == 0o700
            supervisor.cancel(job["id"])
            for path in (root / job["id"]).iterdir():
                assert path.stat().st_mode & 0o077 == 0, path
            assert not list(root.glob(".submit-*"))
        finally:
            supervisor.close()
    finally:
        os.umask(previous)


@posix_only
def test_real_worker_files_are_private_under_a_permissive_parent_umask(tmp_path):
    from test_world_worker import collect, start_worker
    previous = os.umask(0o022)
    try:
        process, receive, directory, _ = start_worker(tmp_path)
    finally:
        os.umask(previous)
    collect(process, receive)
    created = [path for path in directory.rglob("*") if path.name != "request.json"]
    assert (directory / "replay" / "manifest.json") in created
    assert [path for path in created if path.stat().st_mode & 0o077] == []


def test_default_status_messages_name_the_wp_undo_command(supervisor_factory):
    supervisor = supervisor_factory()
    done = supervisor.submit(submission())
    wait_status(supervisor, done["id"], "ready")
    supervisor.application(done["id"], "reconstructing", operation=1)
    assert "/wp undo" in supervisor.application(done["id"], "failed", operation=1)["message"]
    finished = supervisor.submit(submission())
    assert "/wp undo" in complete(supervisor, finished["id"])["message"]
    for job in supervisor.jobs.values():
        assert "/undo" not in job["message"].replace("/wp undo", "")


def test_expired_lease_message_names_the_wp_undo_command(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(world_jobs.time, "time", lambda: clock[0])
    supervisor = JobSupervisor({"jobs_dir": str(tmp_path), "world_lease_seconds": 10}, start=False)
    try:
        job = supervisor.submit(submission())
        clock[0] += 11
        supervisor.tick()
        assert "Use /wp undo" in supervisor.get_job(job["id"])["message"]
    finally:
        supervisor.close()


def test_startup_sweeps_trash_and_staging_leftovers_and_never_loads_them(tmp_path):
    first = JobSupervisor({"jobs_dir": str(tmp_path)}, start=False)
    kept = first.submit(submission())
    first.cancel(kept["id"])
    removed = first.submit(submission(OWNER_B))
    first.cancel(removed["id"])
    root = first.directory
    first.close()
    # A crash after the rename but before the delete: a complete, loadable-looking job in trash.
    trash = root / ".trash-deadbeef"
    trash.mkdir()
    (root / removed["id"]).rename(trash / removed["id"])
    (root / ".submit-leftover.tmp").write_bytes(b"{" * 1000)
    restarted = JobSupervisor({"jobs_dir": str(tmp_path)}, start=False)
    try:
        assert set(restarted.jobs) == {kept["id"]}
        assert not trash.exists() and not (root / ".submit-leftover.tmp").exists()
    finally:
        restarted.close()


def test_removal_leaves_no_trash_and_the_ids_stay_gone(supervisor_factory):
    supervisor = supervisor_factory()
    jobs = [supervisor.submit(submission(prompt="crash")) for _ in range(2)]
    for job in jobs:
        wait_status(supervisor, job["id"], "failed")
    assert supervisor.remove_jobs([job["id"] for job in jobs]) == 2
    assert not list(supervisor.directory.glob(".trash-*"))
    assert [path.name for path in supervisor.directory.iterdir()] == ["supervisor.lock"]
    with pytest.raises(ValueError, match="no longer exists"):
        supervisor.remove_jobs([jobs[0]["id"]])


def test_write_private_json_flushes_to_disk_before_the_atomic_rename(tmp_path, monkeypatch):
    events = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(config_module.os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(config_module.os, "replace", lambda a, b: (events.append("replace"), real_replace(a, b))[1])
    target = tmp_path / "deep" / "value.json"
    config_module.write_private_json(target, {"name": "é"})
    assert events == ["fsync", "replace"]
    assert target.read_text(encoding="utf-8") == '{\n  "name": "\\u00e9"\n}\n'
    compact = tmp_path / "compact.json"
    config_module.write_private_json(compact, {"name": "é"}, indent=None, ensure_ascii=False, create_parent=False)
    assert compact.read_text(encoding="utf-8") == '{"name": "é"}'
    with pytest.raises(OSError):
        config_module.write_private_json(tmp_path / "missing" / "x.json", {}, create_parent=False)
    assert not (tmp_path / "missing").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["compact.json", "deep"]
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_job_metadata_uses_the_shared_private_writer(supervisor_factory, tmp_path):
    supervisor = supervisor_factory()
    job = supervisor.submit(submission(prompt="é"))
    text = (job_directory(tmp_path, job) / "job.json").read_text(encoding="utf-8")
    assert "é" in text and "\n" not in text
    assert not hasattr(world_jobs, "write_json")


@posix_only
@pytest.mark.parametrize("mode, warns", [(0o600, False), (0o400, False), (0o640, False),
                                         (0o644, True), (0o604, True), (0o660, True), (0o666, True)])
def test_token_file_warning_flags_exposure_beyond_owner_and_read_only_group(tmp_path, mode, warns):
    path = tmp_path / "adapter-token.txt"
    path.write_text("x" * 40 + "\n")
    path.chmod(mode)
    warning = token_permission_warning(path)
    assert bool(warning) is warns
    if warns:
        assert str(path) in warning and "chmod" in warning


@posix_only
def test_adapter_token_warns_at_startup_but_still_works(tmp_path, capsys):
    path = tmp_path / "shared" / "token.txt"
    path.parent.mkdir()
    path.write_text("y" * 40 + "\n")
    path.chmod(0o644)
    token, resolved = adapter_token(tmp_path, path)
    assert token == "y" * 40 and resolved == path
    assert "accessible to every user" in capsys.readouterr().err
    path.chmod(0o600)
    adapter_token(tmp_path, path)
    assert capsys.readouterr().err == ""


@posix_only
def test_new_adapter_token_is_private_regardless_of_umask(tmp_path, capsys):
    previous = os.umask(0)
    try:
        token, path = adapter_token(tmp_path)
    finally:
        os.umask(previous)
    assert len(token) >= 24 and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert capsys.readouterr().err == ""
