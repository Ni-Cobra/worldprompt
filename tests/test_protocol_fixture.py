"""Golden Core -> mod result payload, shared with the Fabric mod's ReplayPayloadTest.

The committed JSON is what ``replay.read_complete_result`` (plus the job fields
``JobSupervisor.result`` adds) produces for a small deterministic job. The mod's
JUnit test parses the very same file with the real ``ReplayPayload`` parser, so a
change on either side of the wire format breaks a test on both sides.

Regenerate after an intentional protocol change:
    WORLDPROMPT_UPDATE_FIXTURES=1 .venv/bin/python -m pytest tests/test_protocol_fixture.py
"""
import json
import os
from pathlib import Path

from worldprompt_core import replay
from worldprompt_core.core import BlockVolume
from worldprompt_core.usage import token_usage

FIXTURE = Path(__file__).resolve().parents[1] / "fabric/src/test/resources/complete-result.json"

PLAN = {"version": 1, "summary": "Stone slab with a mixed plank layer, glassed over and torch-lit", "operations": [
    {"op": "fill", "min": [0, 0, 0], "max": [5, 0, 3], "block": "minecraft:stone"},
    {"op": "fill", "min": [0, 1, 0], "max": [5, 1, 3],
     "block": ["minecraft:oak_planks", "minecraft:spruce_planks"]},
    {"op": "replace", "min": [0, 0, 0], "max": [5, 1, 3], "match": ["minecraft:oak_planks"],
     "block": "minecraft:glass", "preserve_properties": False},
    {"op": "single", "at": [2, 2, 2], "block": "minecraft:torch"}]}

JOB = {"status": "ready", "provider": "codex", "model": "fixture-model", "reasoning_effort": "medium",
       "fast_mode": False, "model_calls": 2, "draft_plan_summary": "Stone slab with a plank layer",
       "plan_passes": [{"usage": {"input_tokens": 1200, "output_tokens": 300}},
                       {"usage": {"input_tokens": 1500, "output_tokens": 250}}]}


def build_payload(directory):
    replay.compile_replay(BlockVolume.empty((6, 3, 4)), PLAN, directory / "replay")
    payload = replay.read_complete_result(directory / "replay")
    # The extra fields JobSupervisor.result() adds before sending the payload to the mod.
    payload.update({key: JOB[key] for key in ("provider", "model", "reasoning_effort", "fast_mode",
                                              "draft_plan_summary")})
    payload["token_usage"] = token_usage(JOB)
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def test_committed_result_fixture_matches_what_core_produces(tmp_path):
    produced = build_payload(tmp_path)
    if os.environ.get("WORLDPROMPT_UPDATE_FIXTURES") == "1":
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(produced, encoding="utf-8", newline="\n")
    # A Windows checkout may apply CRLF; the JSON content is what must match.
    committed = FIXTURE.read_bytes().decode("utf-8").replace("\r\n", "\n") if FIXTURE.exists() else None
    assert committed == produced, (
        f"{FIXTURE.relative_to(FIXTURE.parents[4])} is out of date with Core's result payload. If the change is "
        "intentional, regenerate it with WORLDPROMPT_UPDATE_FIXTURES=1 python -m pytest "
        "tests/test_protocol_fixture.py, then run `cd fabric && ./gradlew test` so ReplayPayload still parses it.")


def test_fixture_covers_the_wire_features_the_mod_parses(tmp_path):
    payload = json.loads(build_payload(tmp_path))
    assert payload["result_version"] == 2 and payload["run_encoding"] == "base64-u32le"
    assert any(isinstance(op["block"], list) for op in payload["plan"]["operations"])
    assert [op["dynamic_match"] for op in payload["operations"]] == [False, False, True, False]
    assert payload["token_usage"]["total_tokens"] == 3250
    assert payload["draft_plan_summary"], "The fixture is a Double pass job with a draft summary"
