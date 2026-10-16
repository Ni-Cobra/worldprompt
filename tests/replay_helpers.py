"""Decode the complete transport payload for geometry assertions."""
import base64
import struct

from worldprompt_core.replay import read_complete_result


def operation_runs(destination, index):
    payload = read_complete_result(destination)
    raw = base64.b64decode(payload["operations"][index]["runs_base64"], validate=True)
    return [list(run) for run in struct.iter_unpack("<III", raw)]
