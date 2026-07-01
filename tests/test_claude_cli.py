import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from wechat_codex_multi.claude_cli import ClaudeAccumulator, ClaudeCliRunner
from wechat_codex_multi.codex_cli import CodexCancelled


class FakeState:
    def __init__(self, cwd):
        self.cwd = cwd
        self.reset_calls = []
        self.updates = []
        self.session_updates = {}

    def get_session(self, conversation_key, default_cwd, default_codex_account="", default_agent="codex"):
        session = {"cwd": self.cwd or default_cwd, "claudeSessionId": "session-old"}
        session.update(self.session_updates)
        return session

    def reset_session(self, conversation_key, agent="codex"):
        self.reset_calls.append((conversation_key, agent))

    def update_session(self, conversation_key, **updates):
        self.updates.append((conversation_key, updates))


class ClaudeCliRunnerTests(unittest.TestCase):
    def test_accumulator_parses_stream_json_result(self):
        accumulator = ClaudeAccumulator()
        accumulator.handle({"type": "system", "session_id": "session-1"})
        accumulator.handle(
            {
                "type": "assistant",
                "session_id": "session-1",
                "message": {"content": [{"type": "text", "text": "draft"}]},
            }
        )
        accumulator.handle({"type": "result", "session_id": "session-1", "result": "final"})

        self.assertEqual(accumulator.session_id, "session-1")
        self.assertEqual(accumulator.text(), "final")

    def test_run_uses_stream_json_verbose_and_updates_session(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as claude_dir:
            config = {
                "codex": {"workingDirectory": tmp},
                "claude": {
                    "bin": "claude",
                    "model": "sonnet",
                    "effort": "high",
                    "timeoutMs": 1000,
                    "permissionMode": "bypassPermissions",
                    "defaultAccount": "main",
                    "accounts": [{"name": "main", "claudeConfigDir": claude_dir}],
                },
                "media": {"generators": []},
            }
            state = FakeState(tmp)
            process = Mock()
            process.pid = 12345
            process.stdout = [
                '{"type":"system","session_id":"session-new"}\n',
                '{"type":"assistant","session_id":"session-new","message":{"content":[{"type":"text","text":"ok"}]}}\n',
                '{"type":"result","session_id":"session-new","result":"ok"}\n',
            ]
            process.stderr = []
            process.poll.return_value = None
            process.wait.return_value = 0

            with patch("wechat_codex_multi.claude_cli.subprocess.Popen", return_value=process) as popen:
                result = ClaudeCliRunner(config, state).run("conversation-1", "hello")

            self.assertEqual(result, "ok")
            args = popen.call_args.args[0]
            self.assertIn("--verbose", args)
            self.assertEqual(args[args.index("--output-format") + 1], "stream-json")
            self.assertEqual(args[args.index("--model") + 1], "sonnet")
            self.assertEqual(args[args.index("--effort") + 1], "high")
            self.assertEqual(args[args.index("--permission-mode") + 1], "bypassPermissions")
            self.assertEqual(args[args.index("--resume") + 1], "session-old")
            self.assertEqual(args[-1], "hello")
            env = popen.call_args.kwargs["env"]
            self.assertEqual(env["CLAUDE_CONFIG_DIR"], claude_dir)
            self.assertIn("LOCAL_AGENT_MEDIA_OUTBOX", env)
            self.assertNotIn("WECHAT_CODEX_MULTI_MEDIA_OUTBOX", env)
            self.assertEqual(state.updates[-1][1]["claudeSessionId"], "session-new")
            self.assertEqual(state.updates[-1][1]["claudeModel"], "sonnet")
            self.assertEqual(state.updates[-1][1]["claudeEffort"], "high")

    def test_base_args_omits_default_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = ClaudeCliRunner(
                {
                    "codex": {"workingDirectory": tmp},
                    "claude": {"bin": "claude", "timeoutMs": 1000},
                    "media": {"generators": []},
                },
                FakeState(tmp),
            )

            args = runner._base_args("default", "high", "")

        self.assertNotIn("--model", args)
        self.assertEqual(args[args.index("--effort") + 1], "high")

    def test_base_args_maps_ultracode_to_xhigh_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = ClaudeCliRunner(
                {
                    "codex": {"workingDirectory": tmp},
                    "claude": {"bin": "claude", "timeoutMs": 1000},
                    "media": {"generators": []},
                },
                FakeState(tmp),
            )

            args = runner._base_args("claude-fable-5[1m]", "ultracode", "")

        self.assertEqual(args[args.index("--model") + 1], "claude-fable-5[1m]")
        self.assertEqual(args[args.index("--effort") + 1], "xhigh")
        self.assertEqual(args[args.index("--settings") + 1], '{"ultracode":true}')

    def test_resume_with_current_prompt_version_omits_system_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "codex": {"workingDirectory": tmp},
                "claude": {"bin": "claude", "timeoutMs": 1000},
                "media": {"generators": []},
            }
            state = FakeState(tmp)
            runner = ClaudeCliRunner(config, state)
            state.session_updates = {"claudePromptVersion": runner._prompt_version()}
            process = Mock()
            process.pid = 12345
            process.stdout = ['{"type":"result","session_id":"session-old","result":"ok"}\n']
            process.stderr = []
            process.poll.return_value = None
            process.wait.return_value = 0

            with patch("wechat_codex_multi.claude_cli.subprocess.Popen", return_value=process) as popen:
                result = runner.run("conversation-1", "hello")

            self.assertEqual(result, "ok")
            args = popen.call_args.args[0]
            self.assertNotIn("--append-system-prompt", args)
            self.assertEqual(args[args.index("--resume") + 1], "session-old")
            self.assertEqual(args[-1], "hello")
            self.assertEqual(state.updates[-1][1]["claudePromptVersion"], runner._prompt_version())

    def test_resume_with_old_prompt_version_appends_system_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "codex": {"workingDirectory": tmp},
                "claude": {"bin": "claude", "timeoutMs": 1000},
                "media": {"generators": []},
            }
            state = FakeState(tmp)
            state.session_updates = {"claudePromptVersion": "old"}
            process = Mock()
            process.pid = 12345
            process.stdout = ['{"type":"result","session_id":"session-old","result":"ok"}\n']
            process.stderr = []
            process.poll.return_value = None
            process.wait.return_value = 0

            with patch("wechat_codex_multi.claude_cli.subprocess.Popen", return_value=process) as popen:
                result = ClaudeCliRunner(config, state).run("conversation-1", "hello")

            self.assertEqual(result, "ok")
            args = popen.call_args.args[0]
            self.assertIn("--append-system-prompt", args)
            self.assertIn("local_agent_tools", args[args.index("--append-system-prompt") + 1])

    def test_timeout_terminates_process_group_and_resets_claude_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "codex": {"workingDirectory": tmp},
                "claude": {"bin": "claude", "timeoutMs": 1},
                "media": {"generators": []},
            }
            state = FakeState(tmp)
            process = Mock()
            process.pid = 12345
            process.stdout = []
            process.stderr = []
            process.poll.return_value = None
            process.wait.side_effect = [subprocess.TimeoutExpired("claude", 0.001), 0]

            with patch("wechat_codex_multi.claude_cli.subprocess.Popen", return_value=process):
                with patch("wechat_codex_multi.claude_cli.os.killpg") as killpg:
                    runner = ClaudeCliRunner(config, state)
                    with self.assertRaisesRegex(RuntimeError, "没有返回结果"):
                        runner.run("conversation-1", "hello")

            killpg.assert_called_once_with(12345, signal.SIGTERM)
            self.assertEqual(state.reset_calls, [("conversation-1", "claude")])

    def test_cancel_can_preserve_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = FakeState(tmp)
            runner = ClaudeCliRunner(
                {
                    "codex": {"workingDirectory": tmp},
                    "claude": {"bin": "claude", "timeoutMs": 1000},
                    "media": {"generators": []},
                },
                state,
            )

            self.assertFalse(runner.cancel("conversation-1", reset_session=False))
            self.assertEqual(state.reset_calls, [])

    def test_active_runs_reports_running_process_model_and_pid(self):
        state = FakeState(str(Path.cwd()))
        state.session_updates = {"claudeModel": "opus", "claudeEffort": "medium"}
        runner = ClaudeCliRunner(
            {
                "codex": {"workingDirectory": str(Path.cwd())},
                "claude": {"bin": "claude", "timeoutMs": 1000, "model": "sonnet", "effort": "high"},
                "media": {"generators": []},
            },
            state,
        )
        process = Mock()
        process.pid = 12345
        process.poll.return_value = None
        runner._register_process("acct-1:user-1", process)

        self.assertEqual(
            runner.active_runs(),
            [
                {
                    "agent": "claude",
                    "conversationKey": "acct-1:user-1",
                    "pid": 12345,
                    "model": "opus",
                    "effort": "medium",
                }
            ],
        )

    def test_cancelled_run_raises_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "codex": {"workingDirectory": tmp},
                "claude": {"bin": "claude", "timeoutMs": 1000},
                "media": {"generators": []},
            }
            state = FakeState(tmp)
            process = Mock()
            process.pid = 12345
            process.stdout = []
            process.stderr = []
            process.poll.return_value = None
            process.wait.side_effect = [0, 0]

            with patch("wechat_codex_multi.claude_cli.subprocess.Popen", return_value=process):
                runner = ClaudeCliRunner(config, state)
                runner.cancelled_conversations.add("conversation-1")
                with self.assertRaises(CodexCancelled):
                    runner.run("conversation-1", "hello")


if __name__ == "__main__":
    unittest.main()
