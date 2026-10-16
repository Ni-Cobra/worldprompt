"""Regression coverage for terminal IPC, recoverable storage, and usage failures."""
import json
import os
from pathlib import Path
import signal

import pytest

from worldprompt_core import world_jobs
from worldprompt_core.world_jobs import JobSupervisor
from test_world_worker import FAKE_CLI
import sys
from test_world_jobs import OWNER_B, OWNER_C, isolated_worker, submission, wait_status


@pytest.mark.parametrize("failing_step", ["stage", "publish"])
def test_capture_write_failure_allows_retry_with_same_request_id(tmp_path, monkeypatch, failing_step):
    supervisor = JobSupervisor({"jobs_dir": str(tmp_path)}, start=False)
    request = submission()
    real_fdopen, real_replace = os.fdopen, os.replace

    def fail_stage(*args, **kwargs):
        stream = real_fdopen(*args, **kwargs)
        stream.write(b"{")
        stream.close()
        raise OSError("Fixture disk full")

    def fail_publish(source, destination):
        if Path(destination).name == "request.json":
            raise OSError("Fixture disk full")
        return real_replace(source, destination)

    try:
        with monkeypatch.context() as patch:
            if failing_step == "stage":
                patch.setattr(world_jobs.os, "fdopen", fail_stage)
            else:
                patch.setattr(world_jobs.os, "replace", fail_publish)
            with pytest.raises(OSError, match="disk full"):
                supervisor.submit(request)
        assert not supervisor.jobs
        assert not (supervisor.directory / request["request_id"]).exists()
        assert not list(supervisor.directory.glob(".submit-*"))
        job = supervisor.submit(request)
        assert job["id"] == request["request_id"]
        assert json.loads((supervisor.directory / job["id"] / "request.json").read_text())["source"] == request["source"]
    finally:
        supervisor.close()


@pytest.mark.parametrize("invalid_id", [123, {}, []])
def test_corrupt_history_identifier_does_not_prevent_startup(tmp_path, invalid_id):
    request = submission()
    directory = tmp_path / "world" / request["request_id"]
    directory.mkdir(parents=True)
    (directory / "job.json").write_text(json.dumps({"id": invalid_id, "status": "completed"}))
    supervisor = JobSupervisor({"jobs_dir": str(tmp_path)}, start=False)
    try:
        assert not supervisor.jobs
        assert supervisor.submit(submission())["status"] == "queued"
    finally:
        supervisor.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX provider process group cleanup")
@pytest.mark.parametrize("finished", [False, True])
def test_cancellation_drains_provider_lifecycle_records(tmp_path, monkeypatch, finished):
    supervisor = JobSupervisor({"jobs_dir": str(tmp_path)}, start=False)
    job = supervisor.submit(submission())
    packets = [{"kind": "process", "pid": 123456}]
    if finished:
        packets.append({"kind": "process_finished", "pid": 123456})
    packets.append({"kind": "progress", "values": {"status": "planning"}})
    stopped = []
    monkeypatch.setattr(os, "killpg", lambda pid, signum: stopped.append((pid, signum)))

    class Connection:
        def poll(self):
            return bool(packets)
        def recv(self):
            return packets.pop(0)
        def close(self):
            pass

    class Process:
        exitcode = -signal.SIGTERM
        def terminate(self):
            pass
        def is_alive(self):
            return False
        def join(self, timeout):
            pass
        def close(self):
            pass

    supervisor.workers[job["id"]] = {"process": Process(), "connection": Connection()}
    try:
        cancelled = supervisor.cancel(job["id"])
        assert cancelled["status"] == "cancelling"
        assert stopped == ([] if finished else [(123456, signal.SIGTERM)])
        supervisor.tick()
        assert supervisor.get_job(job["id"])["status"] == "failed"
        assert stopped == ([] if finished else [(123456, signal.SIGTERM), (123456, signal.SIGKILL)])
    finally:
        supervisor.close()


@pytest.mark.parametrize("terminal", ["ready", "failed"])
def test_final_ipc_packet_arriving_between_poll_and_process_exit_is_drained(tmp_path, terminal):
    supervisor = JobSupervisor({"jobs_dir": str(tmp_path)}, start=False)
    queued = supervisor.submit(submission())
    packets = []

    class Connection:
        closed = False
        def poll(self):
            return bool(packets)
        def recv(self):
            return packets.pop(0)
        def close(self):
            self.closed = True

    class Process:
        exitcode = 0
        closed = False
        def is_alive(self):
            # The first poll saw no data. The child writes and exits before
            # the supervisor checks its process state, forcing the real race.
            packets.extend([{"kind": "process", "pid": 123456},
                            {"kind": "process_finished", "pid": 123456}])
            if terminal == "ready":
                packets.append({"kind": "ready", "manifest": {"operation_count": 2, "dimensions": [3, 2, 2]}})
            else:
                packets.append({"kind": "failed", "message": "Review rejected the candidate"})
            return False
        def join(self, timeout):
            pass
        def close(self):
            self.closed = True

    process, connection = Process(), Connection()
    supervisor.jobs[queued["id"]]["status"] = "planning"
    supervisor.workers[queued["id"]] = {"process": process, "connection": connection}
    try:
        supervisor.tick()
        result = supervisor.get_job(queued["id"])
        assert result["status"] == terminal
        if terminal == "failed":
            assert result["message"] == "Review rejected the candidate"
        else:
            assert result["operation_count"] == 2
        assert not packets
        assert process.closed and connection.closed
        assert not supervisor.workers
    finally:
        supervisor.close()


def test_metadata_write_failure_keeps_watchdog_and_other_jobs_running(tmp_path, monkeypatch):
    supervisor = JobSupervisor({"jobs_dir": str(tmp_path)}, worker_target=isolated_worker)
    original_write = world_jobs.write_private_json
    def unavailable(path, value, **options):
        raise OSError("Fixture metadata volume is full")
    monkeypatch.setattr(world_jobs, "write_private_json", unavailable)
    try:
        first = supervisor.submit(submission())
        second = supervisor.submit(submission(OWNER_B))
        for job in (first, second):
            result = wait_status(supervisor, job["id"], "ready")
            assert result["storage_warning"]
        assert supervisor.thread.is_alive()
        monkeypatch.setattr(world_jobs, "write_private_json", original_write)
        supervisor.application(first["id"], "reconstructing")
        supervisor.application(first["id"], "completed", operation=2)
        assert "storage_warning" not in supervisor.get_job(first["id"])
        path = supervisor.directory / first["id"] / "job.json"
        assert json.loads(path.read_text())["status"] == "completed"
        third = supervisor.submit(submission(OWNER_C))
        wait_status(supervisor, third["id"], "ready")
        assert supervisor.thread.is_alive()
    finally:
        monkeypatch.setattr(world_jobs, "write_private_json", original_write)
        supervisor.close()


def test_failed_review_retains_incomplete_usage_in_world_supervisor(tmp_path):
    fake = tmp_path / "fake_cli.py"
    fake.write_text(FAKE_CLI)
    supervisor = JobSupervisor({"jobs_dir": str(tmp_path / "jobs"),
                                "codex_command": [sys.executable, str(fake), "fail_review"],
                                "timeout_seconds": 20, "high_timeout_seconds": 20})
    request = submission()
    request["effort"] = "high"
    request["source"] = {"dimensions": [7, 6, 8], "palette": ["minecraft:air"], "blocks": [0] * (7 * 6 * 8)}
    try:
        job = supervisor.submit(request)
        result = wait_status(supervisor, job["id"], "failed")
        assert result["usage_incomplete"] is True
        assert result["model_calls"] == 2
        assert [p["phase"] for p in result["plan_passes"]] == ["draft"]
        assert result["usage"] == {"input_tokens": 100, "output_tokens": 50}
        assert (supervisor.directory / job["id"] / "request.json").is_file()
        assert not (supervisor.directory / job["id"] / "replay" / "manifest.json").exists()
    finally:
        supervisor.close()
