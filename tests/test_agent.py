"""Subprocess-level planner protocol checks; these never call a paid model."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from worldprompt_core import agent
from worldprompt_core.agent import AgentConfig, AgentError, HIGH_INSTRUCTIONS, INSTRUCTIONS, REVIEW_INSTRUCTIONS, build_command, check_provider, generate_plan, plan_schema, validate_plan_shape
from worldprompt_core.providers import get_provider


FAKE_CLI = '''import json, pathlib, subprocess, sys, time
mode = sys.argv[1]
if sys.argv[-2:] == ["login", "status"]:
    sys.exit(0)
if mode == "descendant":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    pathlib.Path(sys.argv[2]).write_text(str(child.pid))
    time.sleep(60)
if mode == "log_flood":
    sys.stdout.buffer.write(b"x" * 5_000_000)
    sys.stdout.buffer.flush()
    time.sleep(60)
if mode == "no_read":
    time.sleep(60)
request = sys.stdin.read()
args = sys.argv[2:]
output = pathlib.Path(args[args.index("--output-last-message") + 1])
if mode == "timeout":
    time.sleep(60)
elif mode == "bad_json":
    output.write_text("not json")
elif mode == "huge":
    output.write_text("x" * 10000)
elif mode == "noise":
    # Ids, token counts and ordinary words contain "401"/"auth"/"quota"-like text but are not errors.
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 40101, "output_tokens": 401}, "thread_id": "0192a401-5c4e-7d3a-8401-authored1abc"}))
    print("session 3f2401aa-oauth-author-9401 tokens=401", file=sys.stderr)
    sys.exit(1)
elif mode == "auth":
    print("401 unauthorized SECRET_DO_NOT_EXPOSE", file=sys.stderr)
    sys.exit(1)
else:
    count = 300 if mode == "many_ops" else 1
    output.write_text(json.dumps({"version":1,"summary":"A stone marker","operations":[{"op":"fill","min":[0,1,0],"max":[0,1,0],"block":"minecraft:stone"}] * count}))
    print(json.dumps({"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":50}}))
'''


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fake = self.root / "fake_cli.py"
        self.fake.write_text(FAKE_CLI)
        self.summary = {"dimensions": [4, 4, 4], "palette": [{"block": "minecraft:air", "count": 64}]}

    def config(self, mode="ok", **options):
        return AgentConfig(command=(sys.executable, str(self.fake), mode), **options)

    def test_single_and_block_lists_are_in_planner_contract(self):
        operation = {"op": "single", "at": [1, 2, 3], "block": ["minecraft:stone", "minecraft:dirt"]}
        plan = {"version": 1, "summary": "A mottled block", "operations": [operation]}
        self.assertEqual(validate_plan_shape(plan), plan)
        variants = plan_schema()["properties"]["operations"]["items"]["anyOf"]
        self.assertTrue(any(item["properties"]["op"]["enum"] == ["single"] for item in variants))
        self.assertTrue(any("anyOf" in item["properties"]["block"] for item in variants))
        for bad in ([], ["minecraft:stone"] * 257, ["minecraft:stone", 2]):
            with self.assertRaises(AgentError):
                validate_plan_shape({**plan, "operations": [{**operation, "block": bad}]})

    def test_ellipsoid_variants_require_their_exact_fields(self):
        base = {"min": [0, 0, 0], "max": [4, 4, 4], "block": "minecraft:stone"}
        variants = {item["properties"]["op"]["enum"][0]: item
                    for item in plan_schema()["properties"]["operations"]["items"]["anyOf"]}
        self.assertNotIn("ellipsoid", variants)
        self.assertEqual(set(variants["solid_ellipsoid"]["required"]), set(base) | {"op"})
        self.assertEqual(set(variants["hollow_ellipsoid"]["required"]), set(base) | {"op", "thickness"})
        for operation in ({**base, "op": "solid_ellipsoid"},
                          {**base, "op": "hollow_ellipsoid", "thickness": 2}):
            self.assertEqual(validate_plan_shape({"version": 1, "summary": "test", "operations": [operation]})["operations"][0], operation)
        for operation in ({**base, "op": "ellipsoid", "hollow": True},
                          {**base, "op": "solid_ellipsoid", "thickness": 1},
                          {**base, "op": "hollow_ellipsoid"},
                          {**base, "op": "hollow_ellipsoid", "thickness": True},
                          {**base, "op": "hollow_ellipsoid", "thickness": 0}):
            with self.assertRaises(AgentError):
                validate_plan_shape({"version": 1, "summary": "test", "operations": [operation]})

    def test_cylinder_variants_require_axis_and_thickness(self):
        base = {"min": [0, 0, 0], "max": [4, 4, 4], "block": "minecraft:stone", "axis": "y"}
        variants = {item["properties"]["op"]["enum"][0]: item
                    for item in plan_schema()["properties"]["operations"]["items"]["anyOf"]}
        self.assertEqual(set(variants["solid_cylinder"]["required"]), set(base) | {"op"})
        self.assertEqual(set(variants["hollow_cylinder"]["required"]), set(base) | {"op", "thickness"})
        self.assertEqual(variants["solid_cylinder"]["properties"]["axis"]["enum"], ["x", "y", "z"])
        for operation in ({**base, "op": "solid_cylinder"}, {**base, "op": "hollow_cylinder", "thickness": 1}):
            validate_plan_shape({"version": 1, "summary": "test", "operations": [operation]})
        for operation in ({**base, "op": "solid_cylinder", "axis": "up"},
                          {**base, "op": "solid_cylinder", "thickness": 1},
                          {**base, "op": "hollow_cylinder"},
                          {**base, "op": "hollow_cylinder", "thickness": 0}):
            with self.assertRaises(AgentError):
                validate_plan_shape({"version": 1, "summary": "test", "operations": [operation]})

    def test_subprocess_roundtrip_and_usage(self):
        result = generate_plan(self.summary, "add a marker", self.root / "job", self.config(max_output_tokens=40))
        self.assertEqual(result.plan["operations"][0]["block"], "minecraft:stone")
        self.assertEqual(result.usage["input_tokens"], 100)
        self.assertTrue(result.output_target_exceeded)
        self.assertTrue((self.root / "job" / "usage.json").exists())

    def test_normal_retains_instructions_and_default_budget(self):
        config = self.config()
        generate_plan(self.summary, "add a marker", self.root / "normal", config)
        self.assertEqual(config.max_operations, 512)
        self.assertEqual(config.reasoning_effort, "medium")
        self.assertEqual((self.root / "normal" / "instructions.md").read_text(), INSTRUCTIONS)
        request = json.loads((self.root / "normal" / "request.json").read_text())
        self.assertEqual(request["phase"], "normal")
        self.assertIsNone(request["context"])
        self.assertEqual(request["instruction"], "Produce a complete compact edit plan for the requested scope. Use only the provided summary, context and attached previews.")

    def test_high_phases_persist_evidence_and_complete_original_plan_instructions(self):
        config = self.config(timeout_seconds=480, max_operations=512,
                             max_prompt_bytes=128_000, max_plan_bytes=262_144,
                             max_log_bytes=8_000_000, max_output_tokens=10_000,
                             reasoning_effort="high")
        spatial = {"sample_kind": "exact", "columns": [{"x": 0, "z": 0, "surface_y": 0}]}
        draft = generate_plan(self.summary, "build a house", self.root / "draft", config,
                              phase="draft", context={"source_spatial": spatial})
        context = {"source_spatial": spatial, "draft_plan": draft.plan,
                   "draft_validation_error": "Operation 2 leaves the selection",
                   "draft_summary": None, "fit_diagnostics": {"warnings": []}}
        review = generate_plan(self.summary, "build a house", self.root / "review", config,
                               phase="review", context=context)
        self.assertEqual(review.plan, draft.plan)
        for phase, extra in [("draft", HIGH_INSTRUCTIONS), ("review", REVIEW_INSTRUCTIONS)]:
            request = json.loads((self.root / phase / "request.json").read_text())
            self.assertEqual(request["phase"], phase)
            self.assertEqual(request["volume"], self.summary)
            self.assertEqual(request["limits"], {"max_operations": 512, "max_plan_bytes": 262_144, "target_output_tokens": 10_000})
            self.assertEqual((self.root / phase / "instructions.md").read_text(), INSTRUCTIONS + extra)
            schema = json.loads((self.root / phase / "schema.json").read_text())
            self.assertEqual(schema["properties"]["operations"]["maxItems"], 512)
            command = build_command(self.root / phase, config)
            self.assertIn('model_reasoning_effort="high"', command)
        saved_review = json.loads((self.root / "review" / "request.json").read_text())
        self.assertEqual(saved_review["context"], context)
        self.assertIn("ORIGINAL source", saved_review["instruction"])
        self.assertIn("COMPLETE replacement edit plan", REVIEW_INSTRUCTIONS)
        self.assertIn("draft_validation_error", REVIEW_INSTRUCTIONS)
        self.assertIn("separate review pass will inspect the rendered candidate", HIGH_INSTRUCTIONS)
        self.assertIn("Choose a workable layout at the requested scale", HIGH_INSTRUCTIONS)
        self.assertIn("ceiling, not a target", HIGH_INSTRUCTIONS)
        self.assertIn("Make one focused review", REVIEW_INSTRUCTIONS)
        self.assertIn("do not restart the design search", REVIEW_INSTRUCTIONS)

    def test_operation_allowance_is_explicit_and_bounded(self):
        with self.assertRaisesRegex(AgentError, "256-operation limit"):
            generate_plan(self.summary, "large structure", self.root / "small",
                          self.config("many_ops", max_operations=256))
        for phase in ("normal", "draft"):
            result = generate_plan(self.summary, "large structure", self.root / phase,
                                   self.config("many_ops"), phase=phase)
            self.assertEqual(len(result.plan["operations"]), 300)
        with self.assertRaisesRegex(ValueError, "512 operations"):
            self.config(max_operations=513)

    def test_user_prompt_is_not_counted_against_the_volume_context_budget(self):
        prompt = "build \u00e9" * 8_000  # about 64,000 UTF-8 bytes, the protocol maximum
        self.assertLessEqual(len(prompt.encode("utf-8")), 64_000)
        for phase, context in (("normal", None), ("draft", {"source_spatial": {}}),
                               ("review", {"draft_plan": {"summary": "x"}})):
            with self.subTest(phase=phase):
                result = generate_plan(self.summary, prompt, self.root / phase, self.config(), phase=phase, context=context)
                self.assertEqual(len(result.plan["operations"]), 1)
                saved = json.loads((self.root / phase / "request.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["request"], prompt.strip())
        # A prompt whose JSON escapes make it larger than 64,000 bytes in the request is still excluded.
        escaped = "\n".join(["x"] * 32_000)
        self.assertLessEqual(len(escaped.encode()), 64_000)
        self.assertGreater(len(json.dumps(escaped).encode()), 64_000)
        self.assertEqual(generate_plan(self.summary, escaped, self.root / "escaped", self.config()).plan["summary"], "A stone marker")
        # The volume context is still bounded, however short the prompt.
        with self.assertRaisesRegex(AgentError, "input limit"):
            generate_plan({**self.summary, "filler": "z" * 70_000}, "x", self.root / "context", self.config())
        with self.assertRaisesRegex(AgentError, "input limit"):
            generate_plan({**self.summary, "filler": "z" * 70_000}, "y" * 64_000, self.root / "both", self.config())

    def test_phase_context_and_phase_input_limits_fail_before_launch(self):
        cases = [({"phase": "maximum"}, "Planning phase"),
                 ({"phase": []}, "Planning phase"),
                 ({"phase": "draft", "context": []}, "Planning context"),
                 ({"phase": "draft", "context": {"invalid": float("nan")}}, "finite JSON"),
                 ({"phase": "review", "context": {"invalid": object()}}, "finite JSON"),
                 ({"phase": "review", "context": {"draft_plan": "x" * 2000}}, "input limit")]
        with patch("worldprompt_core.agent.subprocess.Popen") as popen:
            for index, (kwargs, message) in enumerate(cases):
                with self.subTest(index=index), self.assertRaisesRegex(AgentError, message):
                    generate_plan(self.summary, "test", self.root / str(index),
                                  self.config(max_prompt_bytes=1024), **kwargs)
                self.assertFalse((self.root / str(index)).exists())
            popen.assert_not_called()

    def test_high_phases_keep_four_image_limit(self):
        with patch("worldprompt_core.agent.subprocess.Popen") as popen:
            with self.assertRaisesRegex(AgentError, "four preview"):
                generate_plan(self.summary, "test", self.root / "images", self.config(),
                              [self.fake] * 5, phase="review", context={"draft_plan": {}})
            popen.assert_not_called()

    def test_timeout_terminates_process(self):
        start = time.monotonic()
        with self.assertRaisesRegex(AgentError, "exceeded.*seconds"):
            generate_plan(self.summary, "test", self.root / "job", self.config("timeout", timeout_seconds=0.2))
        self.assertLess(time.monotonic() - start, 30)

    def test_large_review_timeout_when_cli_never_reads_stdin(self):
        start = time.monotonic()
        children, inputs = [], []
        from worldprompt_core import agent
        real_popen = agent.subprocess.Popen
        def capture_process(*args, **kwargs):
            inputs.append(kwargs["stdin"])
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child
        with patch("worldprompt_core.agent.subprocess.Popen", side_effect=capture_process):
            with self.assertRaisesRegex(AgentError, "exceeded.*seconds"):
                generate_plan(self.summary, "review", self.root / "no-read",
                              self.config("no_read", timeout_seconds=0.2, max_prompt_bytes=512_000),
                              phase="review", context={"draft_plan": "x" * 300_000})
        self.assertLess(time.monotonic() - start, 30)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode)
        self.assertTrue(inputs[0].closed)

    def test_file_stdin_preserves_exact_request_and_closes_after_success(self):
        from worldprompt_core import agent
        real_popen = agent.subprocess.Popen
        handles = []
        def capture_input(*args, **kwargs):
            request_file = kwargs["stdin"]
            handles.append(request_file)
            self.assertEqual(kwargs["stdout"], agent.subprocess.PIPE)
            self.assertEqual(kwargs["stderr"], agent.subprocess.PIPE)
            self.assertEqual(request_file.read(), (self.root / "file-input" / "request.json").read_bytes())
            request_file.seek(0)
            return real_popen(*args, **kwargs)
        with patch("worldprompt_core.agent.subprocess.Popen", side_effect=capture_input):
            generate_plan(self.summary, "add a lantern → entrée", self.root / "file-input", self.config())
        self.assertTrue(all(handle.closed for handle in handles))

    def test_log_files_cannot_exceed_budget_during_output_burst(self):
        with self.assertRaisesRegex(AgentError, "log size limit"):
            generate_plan(self.summary, "test", self.root / "log-flood",
                          self.config("log_flood", max_log_bytes=1024))
        job = self.root / "log-flood"
        self.assertLessEqual((job / "events.jsonl").stat().st_size + (job / "stderr.log").stat().st_size, 1024)

    @unittest.skipUnless(sys.platform == "linux", "POSIX process-group regression")
    def test_timeout_terminates_provider_descendant(self):
        pid_file = self.root / "descendant.pid"
        config = AgentConfig(command=(sys.executable, str(self.fake), "descendant", str(pid_file)),
                             timeout_seconds=0.5)
        with self.assertRaisesRegex(AgentError, "exceeded.*seconds"):
            generate_plan(self.summary, "test", self.root / "descendant", config)
        self.assertTrue(pid_file.exists())
        pid = int(pid_file.read_text())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = Path(f"/proc/{pid}/stat")
            if not status.exists() or status.read_text().split()[2] == "Z":
                break
            time.sleep(.02)
        else:
            self.fail("Provider descendant survived cancellation")

    def test_malformed_or_oversize_output_is_rejected(self):
        for mode, message in [("bad_json", "malformed JSON"), ("huge", "plan size")]:
            with self.subTest(mode=mode), self.assertRaisesRegex(AgentError, message):
                generate_plan(self.summary, "test", self.root / mode, self.config(mode, max_plan_bytes=1024))

    def test_cli_error_is_actionable_and_does_not_expose_raw_auth_output(self):
        with self.assertRaises(AgentError) as caught:
            generate_plan(self.summary, "test", self.root / "job", self.config("auth"))
        self.assertIn("codex login", str(caught.exception))
        self.assertNotIn("SECRET", str(caught.exception))

    def test_unrelated_ids_and_counts_do_not_produce_auth_hints(self):
        with self.assertRaises(AgentError) as caught:
            generate_plan(self.summary, "test", self.root / "job", self.config("noise"))
        self.assertIn("Inspect the private job logs", str(caught.exception))
        self.assertNotIn("login", str(caught.exception))

    def detail(self, stderr="", events=(), provider="codex"):
        (self.root / "stderr.log").write_text(stderr, encoding="utf-8")
        (self.root / "events.jsonl").write_text("".join(
            (line if isinstance(line, str) else json.dumps(line)) + "\n" for line in events), encoding="utf-8")
        return get_provider(provider).failure_detail(self.root / "stderr.log", self.root / "events.jsonl")

    def test_failure_detail_ignores_substrings_in_ids_data_and_ordinary_events(self):
        generic = "Inspect the private job logs"
        noise_events = [
            {"type": "turn.completed", "usage": {"input_tokens": 401, "output_tokens": 40100}, "id": "9401abcd-401-4012"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "The author's oauth model schema was not found; quota, 401 unauthorized and authentication are only words in my plan"}},
            {"type": "result", "subtype": "success", "is_error": False, "result": "authentication 401 rate limit"},
            {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True, "content": "unauthorized 401 usage limit"}]}},
            "truncated {\"type\": \"error\", \"message\": \"401 unauthorized",
        ]
        for stderr in ("", "id 0192a401-aaaa-4b0d-8401-5c4e7d3a9f10 tokens=401 oauth author coauthor\n",
                       "retrying in 1401 ms; used 4,012 tokens\n", "pid 1401: model loaded\n"):
            with self.subTest(stderr=stderr):
                self.assertTrue(self.detail(stderr, noise_events).startswith(generic))

    def test_failure_detail_reads_structured_error_fields(self):
        login = {"codex": "codex login", "claude": "claude auth login"}
        cases = [
            ("codex", [{"type": "error", "message": "unexpected status 401 Unauthorized: Missing bearer"}], "codex login"),
            ("codex", [{"type": "turn.failed", "error": {"message": "Not logged in. Please run codex login"}}], "codex login"),
            ("claude", [{"type": "result", "subtype": "success", "is_error": True, "result": "Invalid API key", "api_error_status": 401}], "claude auth login"),
            ("claude", [{"type": "assistant", "message": {"content": []}, "error": "authentication_failed"}], "claude auth login"),
            ("claude", [{"type": "system", "subtype": "api_retry", "error": "unknown", "error_status": 401}], "claude auth login"),
            ("claude", [{"type": "assistant", "error": "rate_limit"}], "usage limit"),
            ("codex", [{"type": "turn.failed", "error": {"message": "You've hit your usage limit."}}], "usage limit"),
            ("claude", [{"type": "result", "is_error": True, "result": "x", "api_error_status": 429}], "usage limit"),
            ("codex", [{"type": "error", "message": "The model `nope` does not exist"}], "model is unavailable"),
            ("codex", [{"type": "error", "message": "unknown option --flag"}], "incompatible"),
            ("codex", [{"type": "error", "message": "invalid schema for response_format"}], "structured output schema"),
        ]
        for provider, events, expected in cases:
            with self.subTest(events=events):
                text = self.detail("", events, provider)
                self.assertIn(expected, text)
                if expected.endswith("login"):
                    self.assertIn(login[provider], text)

    def test_failure_detail_matches_whole_words_in_stderr(self):
        for stderr, expected in (("Error: 401 Unauthorized\n", "codex login"), ("HTTP 401\n", "codex login"),
                                 ("authentication failed\n", "codex login"), ("Please run /login\n", "codex login"),
                                 ("hit the usage_limit for this plan\n", "usage limit"),
                                 ("unknown option --safe-mode\n", "incompatible"),
                                 ("model gpt-x is not supported\n", "model is unavailable")):
            with self.subTest(stderr=stderr):
                self.assertIn(expected, self.detail(stderr))
        # "model" and a problem phrase on different lines are unrelated.
        self.assertIn("Inspect the private job logs", self.detail("loading model\ncommand not found\n"))

    def test_input_and_job_reuse_are_rejected_before_launch(self):
        with patch("worldprompt_core.agent.subprocess.Popen") as popen:
            with self.assertRaisesRegex(AgentError, "input limit"):
                generate_plan(self.summary, "test", self.root / "job", self.config(max_prompt_bytes=10))
            with self.assertRaisesRegex(AgentError, "unique job"):
                generate_plan(self.summary, "test", self.root, self.config())
            popen.assert_not_called()

    def test_protocol_rejects_unknown_operations_extra_fields_and_bool_coords(self):
        valid = {"version": 1, "summary": "test", "operations": []}
        for op in [{"op": "exec"}, {"op": []}, {"op": "fill", "min": [False, 0, 0], "max": [1, 1, 1], "block": "minecraft:stone"}, {"op": "fill", "min": [0, 0, 0], "max": [1, 1, 1], "block": "minecraft:stone", "code": "print(1)"}]:
            with self.assertRaises(AgentError):
                validate_plan_shape({**valid, "operations": [op]})

    def test_safe_command_and_windows_path_translation(self):
        config = AgentConfig(command=("/mnt/c/codex.exe",))
        # A Windows CLI reaches the planner tools through wsl.exe, which needs the distro name WSL sets.
        with patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"}), patch("worldprompt_core.wsl.cli_path", side_effect=lambda p, c: "C:\\jobs\\" + p.name):
            command = build_command(self.root / "job", config)
        self.assertIn("read-only", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("shell_tool", command)
        self.assertNotIn("--model", command)  # No model configured: the CLI uses its own default.
        self.assertIn("C:\\jobs\\schema.json", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        with patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"}), patch("worldprompt_core.wsl.cli_path", side_effect=lambda p, c: "C:\\jobs\\" + p.name):
            command = build_command(self.root / "job2", replace(config, model="gpt-6-astra"))
        self.assertEqual(command[command.index("--model") + 1], "gpt-6-astra")

    @unittest.skipIf(os.name == "nt", "a native Windows CLI is launched directly, without wsl.exe")
    def test_windows_cli_requires_wsl_distro_name(self):
        config = AgentConfig(command=("/mnt/c/codex.exe",))
        environ = {key: value for key, value in os.environ.items() if key != "WSL_DISTRO_NAME"}
        with patch.dict(os.environ, environ, clear=True), patch("worldprompt_core.wsl.cli_path", side_effect=lambda p, c: "C:\\jobs\\" + p.name):
            with self.assertRaisesRegex(AgentError, "WSL_DISTRO_NAME"):
                build_command(self.root / "job", config)

    def test_login_check_does_not_invoke_exec(self):
        self.assertTrue(check_provider(self.config())["ready"])

    def test_budgets_are_finite_and_integer_where_required(self):
        for options in [{"timeout_seconds": float("nan")}, {"timeout_seconds": float("inf")}, {"max_operations": 2.5}, {"max_log_bytes": True}]:
            with self.assertRaises(ValueError):
                self.config(**options)


if __name__ == "__main__":
    unittest.main()
