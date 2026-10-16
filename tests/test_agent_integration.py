"""Planner protocol through exact volume editing and replay; no model requests."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

from worldprompt_core.agent import AgentConfig, generate_plan
from worldprompt_core.core import BlockVolume, apply_plan
from worldprompt_core.replay import compile_replay
from replay_helpers import operation_runs


PROOF_PLAN = {"version": 1, "summary": "Place a polished andesite marker at [1,1,1].", "operations": [{"op": "fill", "min": [1, 1, 1], "max": [1, 1, 1], "block": "minecraft:polished_andesite"}]}


class AgentCoreIntegrationTests(unittest.TestCase):
    def test_planner_subprocess_preserves_source_and_compiles_exact_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stub = root / "captured_planner.py"
            stub.write_text("import pathlib,sys,json\nsys.stdin.read()\npathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(" + repr(json.dumps(PROOF_PLAN)) + ")\nprint(json.dumps({'type':'turn.completed','usage':{'input_tokens':5620,'output_tokens':69}}))\n")
            source = BlockVolume.empty((4, 4, 4), data_version=5000)
            source.blocks[0] = source.palette_id("minecraft:stone")
            original = source.blocks.copy()
            result = generate_plan(source.summary(), "Place a marker", root / "job", AgentConfig(command=(sys.executable, str(stub))))
            edited, report = apply_plan(source, result.plan)
            self.assertEqual(edited.dimensions, source.dimensions)
            self.assertEqual(edited.data_version, 5000)
            self.assertEqual(report["changed_blocks"], 1)
            self.assertEqual(edited.palette[int(edited.blocks[1, 1, 1])], "minecraft:polished_andesite")
            np.testing.assert_array_equal(source.blocks, original)
            manifest = compile_replay(source, result.plan, root / "replay").manifest
            actual = original.copy().ravel()
            for index in range(manifest["operation_count"]):
                for start, length, palette in operation_runs(root / "replay", index):
                    actual[start:start + length] = palette
            np.testing.assert_array_equal(actual, edited.blocks.ravel())


if __name__ == "__main__":
    unittest.main()
