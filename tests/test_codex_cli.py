import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from wechat_codex_multi.codex_cli import CodexAccumulator, CodexCancelled, CodexCliRunner


class FakeState:
    def __init__(self, cwd):
        self.cwd = cwd
        self.reset_keys = []
        self.updates = []
        self.session_updates = {}

    def get_session(self, conversation_key, default_cwd, default_codex_account=""):
        session = {"cwd": self.cwd or default_cwd, "codexThreadId": "thread-1"}
        session.update(self.session_updates)
        return session

    def reset_session(self, conversation_key):
        self.reset_keys.append(conversation_key)

    def update_session(self, conversation_key, **updates):
        self.updates.append((conversation_key, updates))


class CodexCliRunnerTests(unittest.TestCase):
    def test_accumulator_converts_generated_image_event_to_send_action(self):
        with tempfile.TemporaryDirectory() as codex_home:
            accumulator = CodexAccumulator("thread-1", codex_home=codex_home)
            accumulator.handle({"type": "image_generation_end", "call_id": "ig_abc123"})

            expected = Path(codex_home) / "generated_images" / "thread-1" / "ig_abc123.png"
            self.assertEqual(accumulator.text(), f"[[send_image:{expected}]]")

    def test_timeout_terminates_process_group_and_resets_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "codex": {
                    "bin": "codex",
                    "workingDirectory": tmp,
                    "timeoutMs": 1,
                    "bypassApprovalsAndSandbox": True,
                },
                "media": {"generators": []},
            }
            state = FakeState(tmp)
            process = Mock()
            process.pid = 12345
            process.stdout = []
            process.stderr = []
            process.poll.return_value = None
            process.wait.side_effect = [subprocess.TimeoutExpired("codex", 0.001), 0]

            with patch("wechat_codex_multi.codex_cli.subprocess.Popen", return_value=process) as popen:
                with patch("wechat_codex_multi.codex_cli.os.killpg") as killpg:
                    runner = CodexCliRunner(config, state)
                    with self.assertRaisesRegex(RuntimeError, "没有返回结果"):
                        runner.run("conversation-1", "hello")

            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            killpg.assert_called_once_with(12345, signal.SIGTERM)
            self.assertEqual(state.reset_keys, ["conversation-1"])
            self.assertEqual(runner.processes, set())

    def test_terminate_all_cleans_registered_processes(self):
        runner = CodexCliRunner(
            {
                "codex": {
                    "bin": "codex",
                    "workingDirectory": str(Path.cwd()),
                    "timeoutMs": 1000,
                }
            },
            FakeState(str(Path.cwd())),
        )
        process = Mock()
        process.pid = 12345
        process.poll.return_value = None
        process.wait.return_value = 0
        runner._register_process("conversation-1", process)

        with patch("wechat_codex_multi.codex_cli.os.killpg") as killpg:
            runner.terminate_all()

        killpg.assert_called_once_with(12345, signal.SIGTERM)

    def test_cancel_can_preserve_session(self):
        state = FakeState(str(Path.cwd()))
        runner = CodexCliRunner(
            {
                "codex": {
                    "bin": "codex",
                    "workingDirectory": str(Path.cwd()),
                    "timeoutMs": 1000,
                }
            },
            state,
        )

        self.assertFalse(runner.cancel("conversation-1", reset_session=False))
        self.assertEqual(state.reset_keys, [])

    def test_cancelled_run_raises_cancelled_without_retrying(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "codex": {
                    "bin": "codex",
                    "workingDirectory": tmp,
                    "timeoutMs": 1000,
                    "bypassApprovalsAndSandbox": True,
                },
                "media": {"generators": []},
            }
            state = FakeState(tmp)
            process = Mock()
            process.pid = 12345
            process.stdout = []
            process.stderr = []
            process.poll.return_value = None
            process.wait.side_effect = [0, 0]

            with patch("wechat_codex_multi.codex_cli.subprocess.Popen", return_value=process):
                runner = CodexCliRunner(config, state)
                runner.cancelled_conversations.add("conversation-1")
                with self.assertRaises(CodexCancelled):
                    runner.run("conversation-1", "hello")

            self.assertEqual(state.reset_keys, [])

    def test_run_sets_codex_home_for_selected_account(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as codex_home:
            config = {
                "codex": {
                    "bin": "codex",
                    "workingDirectory": tmp,
                    "timeoutMs": 1000,
                    "bypassApprovalsAndSandbox": True,
                    "defaultAccount": "alt",
                    "accounts": [{"name": "alt", "codexHome": codex_home}],
                },
                "media": {"generators": []},
            }
            state = FakeState(tmp)
            process = Mock()
            process.pid = 12345
            process.stdout = [
                '{"type":"thread.started","thread_id":"thread-2"}\n',
                '{"type":"item.completed","item":{"type":"agent_message","id":"msg-1","text":"ok"}}\n',
            ]
            process.stderr = []
            process.poll.return_value = None
            process.wait.return_value = 0

            with patch("wechat_codex_multi.codex_cli.subprocess.Popen", return_value=process) as popen:
                result = CodexCliRunner(config, state).run("conversation-1", "hello")

            self.assertEqual(result, "ok")
            self.assertEqual(popen.call_args.kwargs["env"]["CODEX_HOME"], codex_home)
            self.assertEqual(state.updates[-1][1]["codexAccount"], "alt")

    def test_run_passes_session_model_and_reasoning_effort(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "codex": {
                    "bin": "codex",
                    "workingDirectory": tmp,
                    "timeoutMs": 1000,
                    "bypassApprovalsAndSandbox": True,
                    "model": "gpt-default",
                    "reasoningEffort": "medium",
                },
                "media": {"generators": []},
            }
            state = FakeState(tmp)
            state.session_updates = {"codexModel": "gpt-5.5", "codexReasoningEffort": "high"}
            process = Mock()
            process.pid = 12345
            process.stdout = ['{"type":"item.completed","item":{"type":"agent_message","id":"msg-1","text":"ok"}}\n']
            process.stderr = []
            process.poll.return_value = None
            process.wait.return_value = 0

            with patch("wechat_codex_multi.codex_cli.subprocess.Popen", return_value=process) as popen:
                result = CodexCliRunner(config, state).run("conversation-1", "hello")

            self.assertEqual(result, "ok")
            args = popen.call_args.args[0]
            self.assertIn("-m", args)
            self.assertEqual(args[args.index("-m") + 1], "gpt-5.5")
            self.assertIn("-c", args)
            self.assertEqual(args[args.index("-c") + 1], 'model_reasoning_effort="high"')

    def test_run_returns_generated_image_when_rollout_record_fails(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as codex_home:
            config = {
                "codex": {
                    "bin": "codex",
                    "workingDirectory": tmp,
                    "timeoutMs": 1000,
                    "bypassApprovalsAndSandbox": True,
                    "defaultAccount": "alt",
                    "accounts": [{"name": "alt", "codexHome": codex_home}],
                },
                "media": {"generators": []},
            }
            state = FakeState(tmp)
            process = Mock()
            process.pid = 12345
            process.stdout = ['{"type":"image_generation_end","call_id":"ig_abc123"}\n']
            process.stderr = [
                "ERROR codex_core::session: failed to record rollout items: thread thread-1 not found\n"
            ]
            process.poll.return_value = None
            process.wait.return_value = 1

            with patch("wechat_codex_multi.codex_cli.subprocess.Popen", return_value=process):
                result = CodexCliRunner(config, state).run("conversation-1", "生成图片")

            expected = Path(codex_home) / "generated_images" / "thread-1" / "ig_abc123.png"
            self.assertEqual(result, f"[[send_image:{expected}]]")


if __name__ == "__main__":
    unittest.main()
