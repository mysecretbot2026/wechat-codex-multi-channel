import signal
import subprocess
import tempfile
import unittest
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
            self.assertEqual(popen.call_args.kwargs["env"]["CLAUDE_CONFIG_DIR"], claude_dir)
            self.assertEqual(state.updates[-1][1]["claudeSessionId"], "session-new")
            self.assertEqual(state.updates[-1][1]["claudeModel"], "sonnet")
            self.assertEqual(state.updates[-1][1]["claudeEffort"], "high")

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
