"""CLI 离线测试：假子进程及本地 Python 进程树，不调用真实 claude。"""
import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from smolagents.models import ChatMessage, MessageRole

from agents.claude_cli_model import (ClaudeCliError, ClaudeCliModel,
                                    ClaudeSubscriptionQuotaExceeded, TRANSCRIPT_HEADER,
                                    MAX_ARGUMENT_BYTES, PROCESS_REAP_TIMEOUT)
from agents.content_stage import ContentAgent, Doc, run_content_stage
from agents.retry_policy import RetrySchedule, classify_error
from agents.resilient_model import ResilientModel


def success(result="正文", **extra):
    return dict(is_error=False, subtype="success", api_error_status=None, result=result,
                terminal_reason="completed", usage={"input_tokens": 10,
                "cache_creation_input_tokens": 20, "cache_read_input_tokens": 30,
                "output_tokens": 40}, **extra)


class FakeProcess:
    """模拟 communicate 的超时、终止与回收，禁止运行任何真实命令。"""

    def __init__(self, payload=None, *, stdout=None, stderr="", returncode=0, timeout=False):
        self.stdout = json.dumps(payload if payload is not None else success(), ensure_ascii=False) \
            if stdout is None else stdout
        self.stderr = stderr
        self.returncode = returncode
        self.timeout = timeout
        self.calls = []
        self.killed = self.reaped = False
        self.pid = 123456789
        self.waits = []

    def communicate(self, input=None, timeout=None):
        self.calls.append((input, timeout))
        if self.timeout and not self.killed:
            raise subprocess.TimeoutExpired("fake-claude", timeout, output=b"partial", stderr=b"partial err")
        self.reaped = True
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.reaped = True
        return self.returncode


class ClaudeCliTests(unittest.TestCase):
    def setUp(self):
        self.process = FakeProcess()
        self.spawned = []

        def spawn(args, **kwargs):
            self.assertTrue(Path(kwargs["cwd"]).is_dir())
            self.assertEqual(list(Path(kwargs["cwd"]).iterdir()), [])
            self.spawned.append((args, kwargs))
            return self.process

        self.popen = patch("agents.claude_cli_model.subprocess.Popen", side_effect=spawn).start()
        self.killpg = patch("agents.claude_cli_model.os.killpg",
                            side_effect=lambda pid, sig: self.process.kill()).start()
        self.addCleanup(patch.stopall)

    def generate(self, **kwargs):
        return ClaudeCliModel("sonnet", owner="Draft").generate(
            [{"role": "user", "content": "请起草"}], **kwargs)

    def test_constructor_has_no_process_or_directory_side_effect(self):
        with patch("agents.claude_cli_model.tempfile.TemporaryDirectory") as temp:
            with patch.dict(os.environ, {"CLAUDE_CLI_PATH": "/fake/claude"}):
                model = ClaudeCliModel("fable", effort="max")
        self.assertEqual(model.cli_path, "/fake/claude")
        temp.assert_not_called()
        self.popen.assert_not_called()

    def test_arguments_stdin_environment_and_temporary_cwd(self):
        original_cwd = os.getcwd()
        with patch.dict(os.environ, {"CLAUDECODE": "parent", "ANTHROPIC_API_KEY": "SECRET",
                                   "ANTHROPIC_AUTH_TOKEN": "TOKEN", "ANTHROPIC_BASE_URL": "bad",
                                   "ANTHROPIC_CUSTOM_HEADERS": "bad", "ANTHROPIC_FUTURE": "bad",
                                   "KEEP_CLI_ENV": "yes"}):
            model = ClaudeCliModel("vendor/model && text", effort="high", cli_path="/fake/claude")
            response = model.generate([ChatMessage(role=MessageRole.SYSTEM, content="SYSTEM"),
                                       ChatMessage(role=MessageRole.USER, content="原样 <file>\n正文")])
            self.assertEqual(os.environ["CLAUDECODE"], "parent")
        args, kw = self.spawned[0]
        self.assertEqual(args, ["/fake/claude", "-p", "--output-format", "json", "--tools", "",
                               "--safe-mode", "--no-session-persistence", "--model", "vendor/model && text",
                               "--effort", "high", "--system-prompt", "SYSTEM"])
        self.assertFalse(kw.get("shell", False))
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(self.process.calls, [("原样 <file>\n正文", 600.0)])
        self.assertEqual(kw["env"]["KEEP_CLI_ENV"], "yes")
        self.assertNotIn("CLAUDECODE", kw["env"])
        self.assertFalse(any(k.startswith("ANTHROPIC_") for k in kw["env"]))
        self.assertEqual(kw["stdin"], subprocess.PIPE)
        self.assertEqual(kw["stdout"], subprocess.PIPE)
        self.assertEqual(kw["stderr"], subprocess.PIPE)
        self.assertFalse(Path(kw["cwd"]).exists())
        self.assertEqual(os.getcwd(), original_cwd)
        self.assertNotIn("--bare", args)
        self.assertEqual(response.content, "正文")

    def test_system_merge_text_blocks_and_no_system(self):
        ClaudeCliModel("opus").generate([
            {"role": "system", "content": "S1"},
            {"role": "system", "content": [{"type": "text", "text": "S2"},
                                            {"type": "image", "text": "IGNORE"}]},
            {"role": "user", "content": [{"type": "text", "text": "A"}, "B"]}])
        self.assertEqual(self.spawned[0][0][-2:], ["--system-prompt", "S1\n\nS2"])
        self.assertEqual(self.process.calls[0][0], "A\nB")
        self.generate()
        self.assertNotIn("--system-prompt", self.spawned[1][0])
        self.assertNotIn("--effort", self.spawned[1][0])

    def test_content_stage_can_supply_materials_by_directory(self):
        self.process = FakeProcess(success('<file name="out.md">\n正文\n</file>'))

        def spawn(args, **kwargs):
            cwd = Path(kwargs["cwd"])
            self.assertEqual(args[args.index("--tools") + 1], "Read,Glob,Grep")
            self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
            self.assertEqual(args[args.index("--allowedTools") + 1], "Read(./materials/**),Glob,Grep")
            self.assertEqual((cwd / "materials/01.md").read_text(encoding="utf-8"), "材料原文")
            system_file = Path(args[args.index("--system-prompt-file") + 1])
            self.assertTrue(system_file.read_text(encoding="utf-8").endswith("系统指令"))
            self.spawned.append((args, kwargs))
            return self.process

        self.popen.side_effect = spawn
        with patch.dict(os.environ, {"CLAUDE_CLI_READ_MATERIALS": "1"}), tempfile.TemporaryDirectory() as output:
            model = ResilientModel(ClaudeCliModel("sonnet"), RetrySchedule())
            stage = run_content_stage(ContentAgent(model, "系统指令", "Draft"), "起草",
                                      [Doc("材料.md", text="材料原文")], ["out.md"], output)
            self.assertEqual(stage.written, ["out.md"])
            self.assertEqual((Path(output) / "out.md").read_text(encoding="utf-8"), "正文\n")
        prompt, _ = self.process.calls[0]
        self.assertIn('<document name="材料.md" path="materials/01.md"/>', prompt)
        self.assertNotIn("材料原文", prompt)
        self.assertFalse(Path(self.spawned[0][1]["cwd"]).exists())

    def test_multi_turn_transcript_preserves_roles_and_order(self):
        ClaudeCliModel("sonnet").generate([
            {"role": "system", "content": "SYS"}, {"role": "user", "content": "FIRST"},
            {"role": "assistant", "content": "PREVIOUS"}, {"role": "user", "content": "LAST"}])
        self.assertEqual(self.process.calls[0][0], TRANSCRIPT_HEADER + '\n\n<turn role="user">\nFIRST\n</turn>'
                         '\n\n<turn role="assistant">\nPREVIOUS\n</turn>\n\n<turn role="user">\nLAST\n</turn>')
        self.assertNotIn("SYS", self.process.calls[0][0])

    def test_success_usage_and_single_stream_delta(self):
        self.process = FakeProcess(success("全文", total_cost_usd=0.01, duration_ms=3))
        response = self.generate()
        self.assertEqual(response.role, MessageRole.ASSISTANT)
        self.assertEqual(response.content, "全文")
        self.assertEqual(response.token_usage.input_tokens, 60)
        self.assertEqual(response.token_usage.output_tokens, 40)
        self.assertEqual(response.raw["duration_ms"], 3)
        deltas = list(ClaudeCliModel("sonnet").generate_stream([{"role": "user", "content": "u"}]))
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0].content, "全文")
        self.assertEqual(deltas[0].token_usage.input_tokens, 60)

    def test_error_status_overrides_success_subtype_and_exit_zero(self):
        for returncode in (0, 1):
            with self.subTest(returncode=returncode):
                self.process = FakeProcess(dict(is_error=True, subtype="success", api_error_status=404,
                                                terminal_reason="api_error", result="Unknown model"),
                                           stderr='[claude-code:unrecognized_model] {}', returncode=returncode)
                with self.assertRaises(ClaudeCliError) as caught:
                    self.generate()
                self.assertEqual(caught.exception.status_code, 404)
                self.assertFalse(classify_error(caught.exception).retryable)
        self.process = FakeProcess(dict(is_error=False, api_error_status=404, result="bad"))
        with self.assertRaises(ClaudeCliError):
            self.generate()

    def test_quota_status_and_case_insensitive_result_or_stderr(self):
        for status, result, stderr in ((429, "limit", ""), (None, "USAGE LIMIT exceeded", ""),
                                       (None, "Limit Reached", ""), (None, "", "You've HIT YOUR LIMIT")):
            with self.subTest(status=status, result=result, stderr=stderr):
                self.process = FakeProcess(dict(is_error=True, subtype="success", api_error_status=status,
                                                result=result), stderr=stderr, returncode=1)
                with self.assertRaises(ClaudeSubscriptionQuotaExceeded) as caught:
                    self.generate()
                error = caught.exception
                self.assertFalse(classify_error(error).retryable)
                self.assertIn("Claude 订阅额度已用尽", str(error))
                self.assertIn("DRAFT_PROVIDER", str(error))
                self.assertIn("等额度恢复后重跑同一条命令", str(error))
                self.assertIn("已完成的内容会续上", str(error))

    def test_authentication_failures_are_permanent(self):
        for status, text in ((401, ""), (403, ""), (None, "Please /LOGIN"),
                             (None, "not logged in"), (None, "Please log in")):
            with self.subTest(status=status, text=text):
                self.process = FakeProcess(dict(is_error=True, api_error_status=status, result=text))
                with self.assertRaises(ClaudeCliError) as caught:
                    self.generate()
                self.assertFalse(classify_error(caught.exception).retryable)
                self.assertIn("先在终端运行 claude 完成登录", str(caught.exception))

    def test_permanent_statuses(self):
        for status in (400, 404, 413, 422):
            with self.subTest(status=status):
                self.process = FakeProcess(dict(is_error=True, api_error_status=status, result="overloaded"))
                with self.assertRaises(ClaudeCliError) as caught:
                    self.generate()
                self.assertFalse(classify_error(caught.exception).retryable)

    def test_transient_failures(self):
        for status, result, stdout in ((500, "", None), (501, "", None), (503, "", None),
                                       (529, "", None), (None, "OVERLOADED", None), (None, "", "bad JSON")):
            with self.subTest(status=status, result=result, stdout=stdout):
                self.process = FakeProcess(dict(is_error=True, api_error_status=status, result=result),
                                           stdout=stdout, returncode=1)
                with self.assertRaises(ClaudeCliError) as caught:
                    self.generate()
                self.assertTrue(classify_error(caught.exception).retryable)

    def test_ordinary_non_json_objects_are_transient_for_all_exit_codes(self):
        for returncode in (0, 1):
            for stdout in ("not JSON", "[]", "null", '"text"', "42"):
                with self.subTest(returncode=returncode, stdout=stdout):
                    self.process = FakeProcess(stdout=stdout, stderr="protocol warning", returncode=returncode)
                    with self.assertRaises(ClaudeCliError) as caught:
                        self.generate()
                    self.assertTrue(classify_error(caught.exception).retryable)

    def test_non_json_quota_text_in_either_pipe_is_permanent_for_all_exit_codes(self):
        for returncode in (0, 1):
            for pipe in ("stdout", "stderr"):
                with self.subTest(returncode=returncode, pipe=pipe):
                    output = {"stdout": "", "stderr": ""}
                    output[pipe] = "Usage limit reached"
                    self.process = FakeProcess(**output, returncode=returncode)
                    with self.assertRaises(ClaudeSubscriptionQuotaExceeded) as caught:
                        self.generate()
                    error = caught.exception
                    self.assertFalse(classify_error(error).retryable)
                    self.assertIn("Claude 订阅额度已用尽", str(error))
                    self.assertIn("等额度恢复后重跑同一条命令", str(error))
                    self.assertIn("DRAFT_PROVIDER", str(error))
                    self.assertIn("Usage limit reached", getattr(error, f"{pipe}_tail"))

    def test_non_json_login_text_in_either_pipe_is_permanent_for_all_exit_codes(self):
        for returncode in (0, 1):
            for pipe in ("stdout", "stderr"):
                with self.subTest(returncode=returncode, pipe=pipe):
                    output = {"stdout": "", "stderr": ""}
                    output[pipe] = "Please /login"
                    self.process = FakeProcess(**output, returncode=returncode)
                    with self.assertRaises(ClaudeCliError) as caught:
                        self.generate()
                    error = caught.exception
                    self.assertFalse(classify_error(error).retryable)
                    self.assertNotIsInstance(error, ClaudeSubscriptionQuotaExceeded)
                    self.assertIn("先在终端运行 claude 完成登录", str(error))
                    self.assertIn("Please /login", getattr(error, f"{pipe}_tail"))

    def test_zero_exit_non_json_output_is_retried_by_model_wrapper(self):
        from agents.resilient_model import ResilientModel
        from agents.retry_policy import RetrySchedule
        broken, working = FakeProcess(stdout="invalid"), FakeProcess()
        self.popen.side_effect = [broken, working]
        model = ResilientModel(ClaudeCliModel("sonnet"), RetrySchedule(delays=(0, 0), jitter_ratio=0),
                               provider="claude_cli")
        result = model.generate([{"role": "user", "content": "u"}])
        self.assertEqual(result.content, "正文")
        self.assertEqual(self.popen.call_count, 2)

    def test_timeout_kills_reaps_and_retains_bounded_diagnostics(self):
        self.process = FakeProcess(stdout="a" * 5000 + "OUT END", stderr="b" * 5000 + "ERR END", timeout=True)
        with self.assertRaises(ClaudeCliError) as caught:
            self.generate()
        error = caught.exception
        self.assertTrue(self.process.killed and self.process.reaped)
        self.killpg.assert_called_once_with(self.process.pid, signal.SIGKILL)
        self.assertEqual(len(self.process.calls), 2)
        self.assertTrue(all(timeout is not None for _, timeout in self.process.calls))
        self.assertTrue(classify_error(error).retryable)
        self.assertTrue(error.stdout_tail.endswith("OUT END") and len(error.stdout_tail) <= 4000)
        self.assertTrue(error.stderr_tail.endswith("ERR END") and len(error.stderr_tail) <= 4000)
        self.assertNotIn("OUT END", str(error))
        self.assertFalse(Path(self.spawned[0][1]["cwd"]).exists())

    def test_reaping_timeout_does_not_replace_original_transient_timeout(self):
        def blocked(input=None, timeout=None):
            self.assertIsNotNone(timeout)
            self.process.calls.append((input, timeout))
            raise subprocess.TimeoutExpired("fake", timeout, output=b"OUT TAIL", stderr=b"ERR TAIL")

        self.process.communicate = blocked
        with patch.object(self.process, "wait", side_effect=subprocess.TimeoutExpired("fake", 0)) as wait:
            with self.assertRaisesRegex(ClaudeCliError, "子进程超时") as caught:
                self.generate()
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.stdout_tail, "OUT TAIL")
        self.assertEqual(caught.exception.stderr_tail, "ERR TAIL")
        self.assertLessEqual(self.process.calls[1][1], PROCESS_REAP_TIMEOUT)
        self.assertLessEqual(wait.call_args.kwargs["timeout"], PROCESS_REAP_TIMEOUT)
        self.assertFalse(Path(self.spawned[0][1]["cwd"]).exists())

    def test_interrupt_terminates_group_and_preserves_interrupt(self):
        original = self.process.communicate

        def interrupted(input=None, timeout=None):
            if not self.process.killed:
                raise KeyboardInterrupt()
            return original(input=input, timeout=timeout)

        self.process.communicate = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.generate()
        self.killpg.assert_called_once_with(self.process.pid, signal.SIGKILL)
        self.assertTrue(self.process.reaped)
        self.assertFalse(Path(self.spawned[0][1]["cwd"]).exists())

    def test_argument_size_preflight_and_e2big_are_permanent(self):
        for text in ("x" * MAX_ARGUMENT_BYTES, "中" * (MAX_ARGUMENT_BYTES // 3 + 1)):
            with self.assertRaisesRegex(ClaudeCliError, "参数长度超限") as caught:
                ClaudeCliModel("sonnet").generate([{"role": "system", "content": text},
                                                   {"role": "user", "content": "u"}])
            self.assertFalse(caught.exception.retryable)
            self.assertIn(str(MAX_ARGUMENT_BYTES), str(caught.exception))
        self.popen.assert_not_called()
        with patch("agents.claude_cli_model.os.sysconf", return_value=64):
            with self.assertRaisesRegex(ClaudeCliError, "ARG_MAX"):
                self.generate()
        self.popen.assert_not_called()
        self.popen.side_effect = OSError(errno.E2BIG, "Argument list too long")
        with self.assertRaisesRegex(ClaudeCliError, "参数长度超限") as caught:
            self.generate()
        self.assertFalse(caught.exception.retryable)

    def test_missing_executable_and_permission_error(self):
        for failure in (FileNotFoundError("missing"), PermissionError("permission")):
            self.popen.side_effect = failure
            with self.assertRaises(ClaudeCliError) as caught:
                self.generate()
            self.assertFalse(classify_error(caught.exception).retryable)
        self.popen.side_effect = FileNotFoundError()
        with self.assertRaisesRegex(ClaudeCliError, "CLAUDE_CLI_PATH"):
            self.generate()

    def test_unsupported_nonempty_parameters_and_invalid_messages(self):
        for kwargs in ({"stop_sequences": ["END"]}, {"response_format": {"type": "json_object"}},
                       {"tools_to_call_from": [object()]}, {"temperature": 0.5}):
            with self.assertRaises(ClaudeCliError) as caught:
                self.generate(**kwargs)
            self.assertFalse(classify_error(caught.exception).retryable)
        for messages in ([], [{"role": "assistant", "content": "history"}],
                         [{"role": "tool-response", "content": "tool"}]):
            with self.assertRaises(ClaudeCliError):
                ClaudeCliModel("sonnet").generate(messages)
        self.popen.assert_not_called()
        self.generate(stop_sequences=[], response_format={}, tools_to_call_from=[])

    def test_invalid_json_schema_and_effort(self):
        for stdout in ("not JSON", "[]", "{}", '{"is_error":false,"result":123}'):
            self.process = FakeProcess(stdout=stdout)
            with self.assertRaises(ClaudeCliError):
                self.generate()
        for effort in ("off", "ultra", ""):
            with self.assertRaises(ValueError):
                ClaudeCliModel("sonnet", effort=effort)

    def test_content_stage_missing_and_lookup_history_through_stream(self):
        from agents.resilient_model import ResilientModel
        from agents.retry_policy import RetrySchedule
        responses = [FakeProcess(success('<lookup key="ref">query</lookup>')),
                     FakeProcess(success('<file name="a.md">A</file>')),
                     FakeProcess(success('<file name="b.md">B</file>'))]
        original_spawn = self.popen.side_effect

        def spawn(args, **kw):
            self.process = responses.pop(0)
            return original_spawn(args, **kw)

        self.popen.side_effect = spawn
        model = ResilientModel(ClaudeCliModel("sonnet"), RetrySchedule(), provider="claude_cli")
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            outcome = run_content_stage(ContentAgent(model, "SYS", "Draft"), "task", [],
                                        ["a.md", "b.md"], folder, lookup=lambda key, query: "EXCERPT")
            self.assertEqual(outcome.missing, [])
            self.assertEqual(outcome.rounds, 3)
            self.assertEqual(Path(folder, "a.md").read_text(), "A\n")
            self.assertEqual(Path(folder, "b.md").read_text(), "B\n")
        self.assertIn("EXCERPT", self.process.calls[0][0])
        self.assertIn('<turn role="assistant">', self.process.calls[0][0])
        self.assertIn("Resend ONLY those blocks", self.process.calls[0][0])


@unittest.skipUnless(sys.platform == "linux", "进程组与 /proc 状态检查使用 Linux")
class RealProcessTreeTests(unittest.TestCase):
    def test_timeout_kills_descendant_holding_pipes_and_removes_cwd(self):
        # 只启动测试目录中的 Python 假 CLI；后代继承 stdout/stderr 并长期占用它们。
        real_popen = subprocess.Popen
        processes = []
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder, "state.json")
            script = Path(folder, "fake-claude")
            script.write_text(
                f"#!{sys.executable}\n"
                "import json, os, pathlib, subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                f"pathlib.Path({str(state)!r}).write_text(json.dumps(dict(parent=os.getpid(), child=child.pid, cwd=os.getcwd())))\n"
                "print('PARENT READY', flush=True)\n"
                "print('STDERR READY', file=sys.stderr, flush=True)\n"
                "time.sleep(30)\n", encoding="utf-8")
            script.chmod(0o700)

            def spawn(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                processes.append(process)
                return process

            start = time.monotonic()
            try:
                with patch("agents.claude_cli_model.subprocess.Popen", side_effect=spawn):
                    with self.assertRaises(ClaudeCliError) as caught:
                        ClaudeCliModel("sonnet", cli_path=str(script), timeout=0.1).generate(
                            [{"role": "user", "content": "u"}])
                elapsed = time.monotonic() - start
                self.assertTrue(caught.exception.retryable)
                self.assertLess(elapsed, 0.1 + PROCESS_REAP_TIMEOUT + 0.35)
                self.assertIn("PARENT READY", caught.exception.stdout_tail)
                self.assertIn("STDERR READY", caught.exception.stderr_tail)
                info = json.loads(state.read_text())
                self.assertFalse(Path(info["cwd"]).exists())
                self.assertIsNotNone(processes[0].returncode)
                status = Path(f'/proc/{info["child"]}/stat')
                if status.exists():
                    self.assertEqual(status.read_text().split(") ", 1)[1][0], "Z",
                                     "后代只能已退出或处于待系统回收的僵尸状态，不能继续运行")
            finally:
                for process in processes:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=1)


if __name__ == "__main__":
    unittest.main()
