"""Native Windows process-tree cleanup; no accounts or model requests."""
import json
import multiprocessing
import os
import subprocess
import sys
import time

import pytest

from process_checks import process_is_running


def contained_worker(report_path):
    from worldprompt_core.process_lifetime import contain_windows_worker
    _job = contain_windows_worker()
    code = (
        "import json,os,pathlib,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({'worker':os.getppid(),"
        "'cli':os.getpid(),'descendant':child.pid})); time.sleep(60)"
    )
    subprocess.run([sys.executable, "-c", code, str(report_path)], check=True)


def temporary_supervisor(report_path):
    worker = multiprocessing.get_context("spawn").Process(target=contained_worker, args=(report_path,))
    worker.start()
    worker.join()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object and parent sentinel regression")
@pytest.mark.parametrize("terminated", ["worker", "supervisor"])
def test_abrupt_exit_cleans_up_cli_descendants(tmp_path, terminated):
    report = tmp_path / "processes.json"
    target = contained_worker if terminated == "worker" else temporary_supervisor
    process = multiprocessing.get_context("spawn").Process(target=target, args=(report,))
    process.start()
    try:
        deadline = time.monotonic() + 15
        while not report.exists() and process.is_alive() and time.monotonic() < deadline:
            time.sleep(.02)
        assert report.exists(), f"Containment fixture exited with {process.exitcode}"
        pids = json.loads(report.read_text())
        assert all(process_is_running(pid) for pid in pids.values())
        process.kill()
        process.join(timeout=5)
        assert not process.is_alive()
        deadline = time.monotonic() + 5
        while any(process_is_running(pid) for pid in pids.values()) and time.monotonic() < deadline:
            time.sleep(.02)
        assert all(not process_is_running(pid) for pid in pids.values())
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        process.close()
