"""Bounded queue and process supervisor for adapter-owned immutable snapshots."""
from __future__ import annotations

import copy
from dataclasses import asdict
import hashlib
import json
import logging
import multiprocessing
import os
from pathlib import Path
import signal
import shutil
import threading
import tempfile
import time
import uuid

from .config import default_config, job_limits, write_private_json
from .block_registry import catalog_from_wire
from .world_worker import run_worker, snapshot_volume
from .context import normalize_viewpoint
from .lecterns import validate_lecterns
from .usage import token_usage
from .limits import MAX_BLOCKS, MAX_MESSAGE_CHARS, MAX_PROMPT_BYTES, SETTING_RANGES, TERMINAL_STATUSES
from .pipeline import agent_config_for, model_settings

PLANNING = {"preparing", "planning", "checking", "reviewing", "validating"}
LOG = logging.getLogger("worldprompt.supervisor")
# Fields the mod's `/wp jobs` listing does not read; the GUI keeps them through list_jobs().
LIST_OMITTED = {"prompt", "lecterns", "summary", "draft_plan_summary", "selection_origin", "dimension",
                "dimensions", "plan_passes", "usage", "draft_validation_error", "initial_fit_diagnostics",
                "preview_warning"}
# Large inputs and outputs that nothing reads once a job is terminal.


class JobNotFound(KeyError):
    """The requested job ID is not (or no longer) known; the only error HTTP maps to 404."""

    def __str__(self):
        return str(self.args[0]) if self.args else "Job not found."


def process_start_time(pid):
    """Linux start time (clock ticks since boot) of ``pid``; None when it cannot be read."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as stream:
            data = stream.read().decode("ascii", "replace")
        # The command name may contain spaces and parentheses; fields resume after the last ")".
        return int(data[data.rindex(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


class PlayerJobLimitError(ValueError):
    """The owner already has `world_max_jobs_per_player` unfinished jobs."""
    code = "player_job_limit"

    def __init__(self, limit):
        self.limit = limit
        super().__init__(f"Core allows at most {limit} active WorldPrompt job{'' if limit == 1 else 's'} per player "
                         f"(world_max_jobs_per_player), and you already have {limit}. "
                         "Wait for one to finish or stop one with /wp cancel <id>.")


class JobSupervisor:
    """Each job gets an isolated worker; only progress crosses IPC."""

    def __init__(self, config, *, worker_target=None, start=True):
        self.config = copy.deepcopy(config)
        settings = {**default_config(), **config}
        self.directory = Path(settings.get("world_jobs_dir", str(Path(
            settings["jobs_dir"]).expanduser() / "world"))).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            self.directory.chmod(0o700)
        limits = job_limits(settings)
        self.concurrency = limits["world_concurrency"]
        self.max_pending = limits["world_max_pending"]
        self.lease_seconds = settings["world_lease_seconds"]
        self.max_jobs_per_player = limits["world_max_jobs_per_player"]
        for key in ("world_concurrency", "world_max_pending", "world_max_jobs_per_player"):
            if type(limits[key]) is not int or limits[key] < 1:
                raise ValueError(f"{key} must be a positive integer.")
        if not isinstance(self.lease_seconds, (float, int)) or not 0 < self.lease_seconds <= SETTING_RANGES["world_lease_seconds"][1]:
            raise ValueError("world_lease_seconds must be positive and at most one day.")
        self.lock = threading.RLock()
        self.jobs = {}
        self._pins = {}
        self._last_tick_error = (None, 0.0)
        self.workers = {}
        self.worker_target = worker_target or run_worker
        self.context = multiprocessing.get_context("spawn")
        self.stopping = threading.Event()
        self.closed = False
        # Same semantics as open("a+b") (appending keeps Windows writes clear of the locked byte), but 0600.
        self._directory_lock = os.fdopen(os.open(self.directory / "supervisor.lock",
                                                 os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600), "a+b")
        try:
            if os.name == "posix":
                import fcntl
                fcntl.flock(self._directory_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt
                self._directory_lock.seek(0)
                self._directory_lock.write(b"0")
                self._directory_lock.flush()
                self._directory_lock.seek(0)
                msvcrt.locking(self._directory_lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self._directory_lock.close()
            raise ValueError("Another supervisor owns this world jobs directory.") from None
        # Leftovers of a crashed removal or submission are never jobs.
        for leftover in list(self.directory.glob(".trash-*")) + list(self.directory.glob(".submit-*")):
            if leftover.is_dir() and not leftover.is_symlink():
                shutil.rmtree(leftover, ignore_errors=True)
            else:
                leftover.unlink(missing_ok=True)
        # A new server session never resumes an unacknowledged world mutation.
        for path in sorted(self.directory.glob("*/job.json")):
            try:
                if os.name == "posix":
                    path.parent.chmod(0o700)
                job = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(job["id"], str) or str(uuid.UUID(job["id"])) != path.parent.name:
                    continue
                if job["status"] not in TERMINAL_STATUSES:
                    job.update(status="failed", message="Core restarted; job was not resumed. Use /wp undo if application had begun.",
                               finished_at=time.time(), updated_at=time.time())
                    self._write_job(path, job)
                self.jobs[job["id"]] = job
            except (OSError, ValueError, KeyError, TypeError):
                continue
        self.thread = None
        if start:
            self.thread = threading.Thread(target=self._watch, name="worldprompt-supervisor", daemon=True)
            self.thread.start()

    @staticmethod
    def _write_job(path, job):
        # Compact JSON; never recreate the directory of a job that was removed meanwhile.
        write_private_json(path, job, indent=None, ensure_ascii=False, create_parent=False)

    def _save(self, job):
        try:
            self._write_job(self.directory / job["id"] / "job.json", job)
            job.pop("storage_warning", None)
        except OSError:
            # A diagnostics write must never kill the queue's watchdog or strand
            # unrelated owners. Result/source writes remain mandatory in workers.
            job["storage_warning"] = "Could not persist the latest job status; recovery after restart may be incomplete."

    @staticmethod
    def _request_details(request):
        viewpoint = request.get("viewpoint") or {}
        return {"prompt": request.get("prompt"),
                "dimensions": request.get("source", {}).get("dimensions"),
                "dimension": viewpoint.get("dimension"),
                "selection_origin": viewpoint.get("selection_origin"),
                "lecterns": request.get("lecterns") or []}

    def _update(self, job, **values):
        if "status" in values and values["status"] != job["status"]:
            job["stage_started_at"] = time.time()
        if values.get("status") == "failed":
            values["usage_incomplete"] = job.get("model_calls", 0) > len(job.get("plan_passes", []))
        job.update(values, updated_at=time.time())
        self._save(job)

    def public(self, job):
        """The job as clients may see it: internal bookkeeping hidden, derived fields added."""
        hidden = {"request_sha256", "lease_at", "cancel_at", "_agent_config"}
        result = {key: copy.deepcopy(value) for key, value in job.items() if key not in hidden}
        result["elapsed_seconds"] = round(max(0, job.get("finished_at", time.time()) - job["started_at"]), 1)
        queued = [j["id"] for j in self.jobs.values() if j["status"] == "queued"]
        result["queue_position"] = queued.index(job["id"]) + 1 if job["id"] in queued else None
        result["token_usage"] = token_usage(job)
        return result

    def _lookup(self, job_id):
        try:
            return self.jobs[str(uuid.UUID(job_id))]
        except (ValueError, TypeError, KeyError, AttributeError):
            raise JobNotFound("Job not found.") from None

    def info(self):
        from . import __version__
        with self.lock:
            planner = agent_config_for(self.config)
            return {"version": __version__, **model_settings(planner),
                    "stopping": self.closed, "job_limits": {**default_config(), **self.config}["job_limits"],
                    "active_jobs": sum(j["status"] not in TERMINAL_STATUSES and j["status"] != "queued" for j in self.jobs.values()),
                    "queued_jobs": sum(j["status"] == "queued" for j in self.jobs.values()),
                    "world_concurrency": self.concurrency, "world_max_pending": self.max_pending,
                    "world_max_jobs_per_player": self.max_jobs_per_player,
                    "max_selection_blocks": MAX_BLOCKS, "max_operations": planner.max_operations,
                    "timeout_seconds": planner.timeout_seconds,
                    "high_timeout_seconds": agent_config_for(self.config, "high").timeout_seconds}

    def submit(self, body):
        if not isinstance(body, dict):
            raise ValueError("Expected a job object.")
        allowed = {"request_id", "owner_id", "owner_name", "prompt", "effort", "source", "viewpoint", "lecterns",
                   "block_catalog"}
        request = copy.deepcopy({key: value for key, value in body.items() if key in allowed})
        if "block_catalog" in request:
            request["block_catalog"] = catalog_from_wire(request["block_catalog"]).to_wire()
        request["adapter"] = "world"
        if request.get("viewpoint") is not None:
            request["viewpoint_context"] = normalize_viewpoint(request["viewpoint"])
        try:
            owner = str(uuid.UUID(request["owner_id"]))
            job_id = str(uuid.UUID(request.get("request_id") or str(uuid.uuid4())))
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ValueError("owner_id and request_id must be UUIDs.") from None
        prompt, name = request.get("prompt"), request.get("owner_name")
        effort = request.get("effort", "normal")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise ValueError(f"Prompt must contain 1–{MAX_PROMPT_BYTES} UTF-8 bytes.")
        if not isinstance(name, str) or not name.strip() or len(name) > 64 or any(ord(c) < 32 for c in name):
            raise ValueError("Invalid player display name.")
        if effort not in ("normal", "high"):
            raise ValueError("Workflow must be Single pass (normal) or Double pass (high).")
        request.update(request_id=job_id, owner_id=owner, prompt=prompt.strip(), effort=effort)
        encoded = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        fingerprint = hashlib.sha256(encoded).hexdigest()
        with self.lock:
            existing = self.jobs.get(job_id)
            if existing:
                if existing.get("request_sha256") != fingerprint:
                    raise ValueError("request_id was already used with different submission data.")
                existing["lease_at"] = time.time()
                return self.public(existing)
        # Validate before occupying a player; model and geometry work stay in the child.
        volume = snapshot_volume(request.get("source"))
        # Optional since the lectern protocol; older mods omit the field.
        validate_lecterns(request.get("lecterns"), volume,
                          (request.get("viewpoint_context") or {}).get("selection_origin"))
        # The capture can be tens of megabytes: write it privately before taking the lock,
        # then publish it with an atomic rename inside.
        descriptor, staged = tempfile.mkstemp(dir=self.directory, prefix=".submit-", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
            with self.lock:
                return self._admit(request, staged, job_id, owner, name, effort, fingerprint)
        finally:
            Path(staged).unlink(missing_ok=True)

    def _admit(self, request, staged, job_id, owner, name, effort, fingerprint):
        if self.closed:
            raise ValueError("The core is shutting down.")
        if job_id in self.jobs:
            existing = self.jobs[job_id]
            if existing.get("request_sha256") != fingerprint:
                raise ValueError("request_id was already used with different submission data.")
            return self.public(existing)
        # Unfinished includes queued, planning, applying and still-stopping jobs.
        active = [j for j in self.jobs.values() if j["status"] not in TERMINAL_STATUSES]
        limit = self.max_jobs_per_player
        if sum(j["owner_id"] == owner for j in active) >= limit:
            raise PlayerJobLimitError(limit)
        if len(active) >= self.max_pending:
            raise ValueError("The job queue is full. Try again when a job finishes.")
        planner = agent_config_for(self.config, effort)
        directory = self.directory / job_id
        directory.mkdir(mode=0o700, exist_ok=False)
        try:
            os.replace(staged, directory / "request.json")
        except OSError:
            # Only remove files created by this failed, unpublished submission.
            (directory / "request.json").unlink(missing_ok=True)
            directory.rmdir()
            raise
        now = time.time()
        job = {"id": job_id, "owner_id": owner, "owner_name": name, "effort": effort,
               **self._request_details(request),
               **model_settings(planner), "_agent_config": asdict(planner),
               "owner_kind": request["adapter"],
               "status": "queued", "message": "Queued for an isolated planner worker.",
               "started_at": now, "stage_started_at": now, "updated_at": now, "lease_at": now,
               "operation": 0, "operation_count": 0, "request_sha256": fingerprint}
        self.jobs[job_id] = job
        self._save(job)
        return self.public(job)

    def _drain_worker(self, job, worker):
        connection = worker["connection"]
        try:
            while connection.poll():
                event = connection.recv()
                # Process ownership must still be tracked during cancellation:
                # the CLI may have started just before we began draining IPC.
                if event.get("kind") == "process" and os.name == "posix":
                    pid = event.get("pid")
                    if type(pid) is int and pid > 0:
                        worker["cli_pid"] = pid
                        # Identifies this exact process, so a recycled PID is never signalled.
                        worker["cli_start"] = process_start_time(pid)
                elif event.get("kind") == "process_finished" and os.name == "posix":
                    if event.get("pid") == worker.get("cli_pid"):
                        worker.pop("cli_pid", None)
                        worker.pop("cli_start", None)
                if job["status"] == "cancelling":
                    continue
                if event.get("kind") == "progress":
                    values = event.get("values", {})
                    allowed = {"status", "message", "model_calls", "plan_passes", "usage", "agent_seconds", "dimensions", "preview_warning",
                               "initial_fit_diagnostics", "draft_validation_error", "summary", "draft_plan_summary"}
                    values = {key: value for key, value in values.items() if key in allowed}
                    if values.get("status", job["status"]) not in PLANNING:
                        raise ValueError("Worker reported an invalid planning stage.")
                    self._update(job, **values)
                elif event.get("kind") == "ready":
                    worker["ready"] = event["manifest"]
                elif event.get("kind") == "failed":
                    worker["error"] = str(event.get("message", "Worker failed."))[:MAX_MESSAGE_CHARS]
        except (EOFError, OSError):
            pass
        except Exception as exc:
            worker["error"] = str(exc)
            worker["process"].terminate()

    def list_jobs(self, brief=False):
        """Unfinished jobs. ``brief`` drops what the Minecraft mod's job list never reads."""
        with self.lock:
            jobs = [self.public(j) for j in self.jobs.values() if j["status"] not in TERMINAL_STATUSES]
        if brief:
            jobs = [{key: value for key, value in job.items() if key not in LIST_OMITTED} for job in jobs]
        return jobs

    def cleanup_blocked_reason(self, job):
        if job["status"] not in TERMINAL_STATUSES or job["id"] in self.workers:
            return "Wait for this job to finish before removing it."
        if job["id"] in self._pins:
            return "Wait for Minecraft to finish downloading this job before removing it."
        return None

    def remove_jobs(self, job_ids):
        """Remove explicitly selected finished history, leaving Minecraft's retained plans untouched."""
        if not isinstance(job_ids, list) or not job_ids or any(not isinstance(i, str) for i in job_ids):
            raise ValueError("Choose finished jobs to remove.")
        with self.lock:
            try:
                jobs = {self._lookup(i)["id"]: self._lookup(i) for i in job_ids}
            except JobNotFound:
                raise ValueError("A selected job no longer exists. Refresh the history.") from None
            for job in jobs.values():
                reason = self.cleanup_blocked_reason(job)
                if reason:
                    raise ValueError(reason)
                if (self.directory / job["id"]).is_symlink():
                    raise ValueError("Cannot remove a linked job directory.")
            # Rename everything into one trash directory first: a failure part-way
            # restores the moved jobs, so history is never left half-deleted.
            trash = self.directory / (".trash-" + uuid.uuid4().hex)
            trash.mkdir(mode=0o700)
            moved = []
            try:
                for job_id in jobs:
                    directory = self.directory / job_id
                    if directory.exists():
                        os.replace(directory, trash / job_id)
                        moved.append(job_id)
            except OSError:
                for job_id in reversed(moved):
                    try:
                        os.replace(trash / job_id, self.directory / job_id)
                    except OSError:
                        LOG.error("Could not restore job %s after a failed removal", job_id, exc_info=True)
                shutil.rmtree(trash, ignore_errors=True)
                raise
            for job_id in jobs:
                del self.jobs[job_id]
        # Deleting is slow and cannot fail the removal; a leftover trash directory is
        # swept when the supervisor next starts and is never loaded as a job.
        shutil.rmtree(trash, ignore_errors=True)
        return len(jobs)

    def get_job(self, job_id):
        with self.lock:
            job = self._lookup(job_id)
            job["lease_at"] = time.time()
            return self.public(job)

    def result(self, job_id):
        from .replay import read_complete_result
        with self.lock:
            job = self._lookup(job_id)
            if job["status"] not in {"ready", "reconstructing", "completed"}:
                raise ValueError("No fully validated final plan is available.")
            # Pin the job instead of holding the lock: the replay can be tens of megabytes,
            # and GUI cleanup must still not remove it mid-transfer.
            self._pins[job["id"]] = self._pins.get(job["id"], 0) + 1
        try:
            result = read_complete_result(self.directory / job["id"] / "replay")
        finally:
            with self.lock:
                self._unpin(job)
        with self.lock:
            result.update({key: job[key] for key in ("provider", "model", "reasoning_effort", "fast_mode",
                                                     "draft_plan_summary") if key in job})
            result["token_usage"] = token_usage(job)
        return result

    def _unpin(self, job):
        remaining = self._pins.get(job["id"], 1) - 1
        if remaining > 0:
            self._pins[job["id"]] = remaining
            return
        self._pins.pop(job["id"], None)

    def application(self, job_id, status, operation=0, message=None):
        with self.lock:
            job = self._lookup(job_id)
            if status not in {"reconstructing", "completed", "failed"}:
                raise ValueError("Invalid application state.")
            if type(operation) is not int or not job["operation"] <= operation <= job["operation_count"]:
                raise ValueError("Operation progress must be monotonic and within the final plan.")
            if job["status"] in TERMINAL_STATUSES:
                if status == job["status"]:
                    return self.public(job)
                raise ValueError("This job is already finished.")
            if job["status"] not in {"ready", "reconstructing"}:
                raise ValueError("The final plan is not ready for application.")
            if status == "completed" and (job["status"] != "reconstructing" or operation != job["operation_count"]):
                raise ValueError("Complete every operation before acknowledging completion.")
            default = {"reconstructing": "Reconstructing the final AI operation sequence.",
                       "completed": "Finished · /wp undo available", "failed": "World application failed; /wp undo is available if blocks changed."}
            values = {"status": status, "operation": operation, "message": str(message or default[status])[:MAX_MESSAGE_CHARS], "lease_at": time.time()}
            if status in TERMINAL_STATUSES:
                values["finished_at"] = time.time()
            self._update(job, **values)
            return self.public(job)

    def cancel(self, job_id, message=None):
        with self.lock:
            job = self._lookup(job_id)
            if job["status"] in TERMINAL_STATUSES or job["status"] == "cancelling":
                return self.public(job)
            reason = str(message or "Job cancelled by its adapter.")[:MAX_MESSAGE_CHARS]
            if job["id"] in self.workers:
                self._update(job, status="cancelling", message=reason, cancel_at=time.monotonic())
                worker = self.workers[job["id"]]
                self._drain_worker(job, worker)
                self._stop_provider_group(worker, signal.SIGTERM)
                worker["process"].terminate()
            else:
                self._update(job, status="failed", message=reason, finished_at=time.time())
            return self.public(job)

    @staticmethod
    def _stop_provider_group(worker, signum):
        if os.name != "posix" or not worker.get("cli_pid"):
            return
        pid = worker["cli_pid"]
        if os.path.isdir("/proc/self"):
            # A recycled PID has a different start time; never signal a stranger's group.
            # No process at all is fine: the group may still hold orphaned children.
            current = process_start_time(pid)
            if current is not None and current != worker.get("cli_start"):
                return
        try:
            os.killpg(pid, signum)
        except OSError:
            # Already gone (ESRCH) or not ours to signal (EPERM); either way nothing to stop.
            pass

    def tick(self):
        with self.lock:
            for job in list(self.jobs.values()):
                if job["status"] not in TERMINAL_STATUSES and job["status"] != "cancelling" and time.time() - job["lease_at"] > self.lease_seconds:
                    self.cancel(job["id"], "Adapter connection expired; job cancelled. Use /wp undo if application had begun.")
            for job_id, worker in list(self.workers.items()):
                job = self.jobs[job_id]
                process, connection = worker["process"], worker["connection"]
                self._drain_worker(job, worker)
                if job["status"] == "cancelling" and process.is_alive() and time.monotonic() - job["cancel_at"] > 8:
                    self._stop_provider_group(worker, getattr(signal, "SIGKILL", signal.SIGTERM))
                    process.kill()
                if process.is_alive():
                    continue
                process.join(timeout=0)
                # A child can send its final record between poll() and exit.
                # Once it is reaped, drain the pipe before deciding its outcome.
                self._drain_worker(job, worker)
                self._stop_provider_group(worker, getattr(signal, "SIGKILL", signal.SIGTERM))
                connection.close()
                del self.workers[job_id]
                if job["status"] == "cancelling":
                    self._update(job, status="failed", finished_at=time.time())
                elif process.exitcode == 0 and worker.get("ready") and not worker.get("error"):
                    manifest = worker["ready"]
                    self._update(job, status="ready", message="Final plan validated; ready for reconstruction.",
                                 operation_count=manifest["operation_count"], dimensions=manifest["dimensions"])
                else:
                    self._update(job, status="failed", message=worker.get("error", f"Worker exited unexpectedly ({process.exitcode})."), finished_at=time.time())
                process.close()
            if self.closed:
                return
            for job in self.jobs.values():
                if len(self.workers) >= self.concurrency:
                    break
                if job["status"] != "queued":
                    continue
                receive = send = None
                try:
                    receive, send = self.context.Pipe(duplex=False)
                    worker_config = dict(copy.deepcopy(self.config), _supervisor_pid=os.getpid())
                    if "_agent_config" in job:
                        worker_config["_agent_config"] = copy.deepcopy(job["_agent_config"])
                    process = self.context.Process(target=self.worker_target,
                        args=(worker_config, self.directory / job["id"] / "request.json", send),
                        name="worldprompt-" + job["id"][:8], daemon=False)
                    process.start()
                    send.close()
                    self.workers[job["id"]] = {"process": process, "connection": receive}
                    self._update(job, status="preparing", message="Preparing source views in an isolated worker.", worker_pid=process.pid)
                except Exception as exc:
                    for end in (send, receive):
                        if end is not None:
                            end.close()
                    self._update(job, status="failed", message=f"Could not start worker: {exc}", finished_at=time.time())

    def _safe_tick(self):
        """One failed pass must never end the watchdog, or every job would stall silently."""
        try:
            self.tick()
        except Exception as exc:
            message, logged_at = self._last_tick_error
            now = time.monotonic()
            # Report a persistent fault once per distinct message and then at most every 30 seconds.
            if message != repr(exc) or now - logged_at > 30:
                LOG.exception("Supervisor pass failed; it will be retried")
                self._last_tick_error = (repr(exc), now)

    def _watch(self):
        while not self.stopping.wait(0.1):
            self._safe_tick()

    def close(self):
        if self.closed:
            return
        with self.lock:
            self.closed = True
            for job in list(self.jobs.values()):
                if job["status"] not in TERMINAL_STATUSES:
                    self.cancel(job["id"], "Core service stopped; job was not resumed.")
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=2)
        deadline = time.monotonic() + 30
        while self.workers and time.monotonic() < deadline:
            self._safe_tick()
            time.sleep(0.02)
        self._directory_lock.close()
