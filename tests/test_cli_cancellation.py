"""Cancellation belongs to one CLI execution, including its retry attempts."""

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from wechat_codex_multi.claude_cli import ClaudeCliRunner
from wechat_codex_multi.codex_cli import CodexCancelled, CodexCliRunner
from wechat_codex_multi.state import StateStore


class CliCancellationTests(unittest.TestCase):
    @contextmanager
    def case(self, agent):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "codex": {"bin": "fixture-codex", "workingDirectory": tmp, "timeoutMs": 1000,
                          "accounts": [{"name": "main", "codexHome": str(Path(tmp) / "codex")}]},
                "claude": {"bin": "fixture-claude", "timeoutMs": 1000,
                           "accounts": [{"name": "main", "claudeConfigDir": str(Path(tmp) / "claude")}]},
                "media": {"generators": []},
            }
            state = StateStore(Path(tmp) / "state")
            state.update_session("conversation", agent=agent, codexThreadId="original-thread",
                                 claudeSessionId="original-session")
            runner = (CodexCliRunner if agent == "codex" else ClaudeCliRunner)(config, state)

            def process(text="ok", error=""):
                child = Mock()
                child.poll.return_value = None
                child.wait.return_value = 1 if error else 0
                if agent == "codex":
                    events = [{"type": "thread.started", "thread_id": "new-thread"},
                              {"type": "error", "message": error} if error else
                              {"type": "item.completed", "item": {"id": "message", "type": "agent_message",
                                                                    "text": text}}]
                else:
                    events = [{"type": "system", "session_id": "new-session"},
                              {"type": "result", "result": error or text, "is_error": bool(error)}]
                child.stdout = [json.dumps(event) + "\n" for event in events]
                child.stderr = []
                return child

            with patch.object(runner, "_resolve_bin", return_value="fixture-cli"), \
                    patch.object(runner, "_terminate_process") as terminate, \
                    patch(f"wechat_codex_multi.{agent}_cli.subprocess.Popen") as popen:
                yield SimpleNamespace(agent=agent, runner=runner, state=state, popen=popen,
                                      terminate=terminate, process=process, cwd=tmp,
                                      session_field="codexThreadId" if agent == "codex" else "claudeSessionId")
            state.flush()

    def test_idle_reset_and_interrupt_leave_next_execution_usable(self):
        for agent in ("codex", "claude"):
            for reset in (False, True):
                with self.subTest(agent=agent, reset=reset), self.case(agent) as c:
                    original = c.state.get_session("conversation", c.cwd)[c.session_field]
                    self.assertFalse(c.runner.cancel("conversation", reset_session=reset))
                    self.assertFalse(c.runner.cancel("conversation", reset_session=reset))
                    self.assertEqual(c.state.get_session("conversation", c.cwd)[c.session_field],
                                     "" if reset else original)
                    c.popen.return_value = c.process()
                    self.assertEqual(c.runner.run("conversation", "next task"), "ok")
                    c.terminate.assert_not_called()

    def test_running_cancel_applies_only_to_that_execution(self):
        for agent in ("codex", "claude"):
            for reset in (False, True):
                with self.subTest(agent=agent, reset=reset), self.case(agent) as c:
                    child = c.process()
                    original = c.state.get_session("conversation", c.cwd)[c.session_field]

                    def wait(timeout):
                        self.assertTrue(c.runner.cancel("conversation", reset_session=reset))
                        return 0

                    child.wait.side_effect = wait
                    c.popen.return_value = child
                    with self.assertRaises(CodexCancelled):
                        c.runner.run("conversation", "cancel me")
                    c.terminate.assert_called_once_with(child)
                    self.assertFalse(c.runner.is_running("conversation"))
                    self.assertEqual(c.state.get_session("conversation", c.cwd)[c.session_field],
                                     "" if reset else original)
                    c.popen.return_value = c.process()
                    self.assertEqual(c.runner.run("conversation", "next task"), "ok")
                    c.popen.return_value = c.process("other result")
                    self.assertEqual(c.runner.run("other", "another conversation"), "other result")

    def test_cancel_during_preparation_prevents_child_launch(self):
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent), self.case(agent) as c:
                get_session = c.state.get_session

                def prepare(*args, **kwargs):
                    session = get_session(*args, **kwargs)
                    self.assertTrue(c.runner.is_running("conversation"))
                    self.assertTrue(c.runner.cancel("conversation", reset_session=False))
                    return session

                with patch.object(c.state, "get_session", side_effect=prepare):
                    with self.assertRaises(CodexCancelled):
                        c.runner.run("conversation", "cancel before spawn")
                c.popen.assert_not_called()
                c.popen.return_value = c.process()
                self.assertEqual(c.runner.run("conversation", "next task"), "ok")

    def test_cancel_during_spawn_terminates_the_new_child(self):
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent), self.case(agent) as c:
                child = c.process()

                def spawn(*args, **kwargs):
                    self.assertTrue(c.runner.cancel("conversation", reset_session=False))
                    return child

                c.popen.side_effect = spawn
                with self.assertRaises(CodexCancelled):
                    c.runner.run("conversation", "cancel during spawn")
                c.terminate.assert_called_once_with(child)
                c.popen.side_effect = None
                c.popen.return_value = c.process()
                self.assertEqual(c.runner.run("conversation", "next task"), "ok")

    def test_cancel_after_child_exit_still_cancels_pending_result(self):
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent), self.case(agent) as c:
                child = c.process()

                def wait(timeout):
                    child.poll.return_value = 0
                    self.assertTrue(c.runner.is_running("conversation"))
                    self.assertTrue(c.runner.cancel("conversation", reset_session=False))
                    return 0

                child.wait.side_effect = wait
                c.popen.return_value = child
                with self.assertRaises(CodexCancelled):
                    c.runner.run("conversation", "cancel while finishing")
                c.terminate.assert_not_called()

    def test_slow_cancel_cannot_reset_or_cancel_a_replacement_execution(self):
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent), self.case(agent) as c:
                started = threading.Event()
                finish = threading.Event()
                cancelling = threading.Event()
                release = threading.Event()
                child = c.process()

                def wait(timeout):
                    started.set()
                    if not finish.wait(2):
                        raise RuntimeError("fixture child did not finish")
                    child.poll.return_value = 0
                    return 0

                def terminate(process):
                    finish.set()
                    cancelling.set()
                    if not release.wait(2):
                        raise RuntimeError("fixture cancel was not released")

                child.wait.side_effect = wait
                c.terminate.side_effect = terminate
                c.popen.side_effect = [child, c.process()]
                with ThreadPoolExecutor(max_workers=2) as pool:
                    old_run = pool.submit(c.runner.run, "conversation", "old task")
                    try:
                        self.assertTrue(started.wait(2))
                        cancel = pool.submit(c.runner.cancel, "conversation", reset_session=True)
                        self.assertTrue(cancelling.wait(2))
                        with self.assertRaises(CodexCancelled):
                            old_run.result(timeout=2)
                        self.assertFalse(cancel.done())
                        self.assertEqual(c.runner.run("conversation", "replacement task"), "ok")
                    finally:
                        finish.set()
                        release.set()
                    self.assertTrue(cancel.result(timeout=2))
                c.terminate.assert_called_once_with(child)
                self.assertEqual(c.state.get_session("conversation", c.cwd)[c.session_field],
                                 "new-thread" if agent == "codex" else "new-session")

    def test_cancel_during_retry_prevents_another_child(self):
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent), self.case(agent) as c:
                error = "stream disconnected before completion" if agent == "codex" else "session not found"
                c.popen.return_value = c.process(error=error)
                get_session = c.state.get_session
                attempts = 0

                def prepare(*args, **kwargs):
                    nonlocal attempts
                    attempts += 1
                    session = get_session(*args, **kwargs)
                    if attempts == 2:
                        self.assertTrue(c.runner.cancel("conversation", reset_session=False))
                    return session

                with patch.object(c.state, "get_session", side_effect=prepare):
                    with self.assertRaises(CodexCancelled):
                        c.runner.run("conversation", "cancel retry")
                self.assertEqual(attempts, 2)
                c.popen.assert_called_once()
                c.popen.return_value = c.process()
                self.assertEqual(c.runner.run("conversation", "next task"), "ok")

    def test_cancel_during_auth_refresh_prevents_retry(self):
        with self.case("codex") as c:
            child = c.process(error="workspace routing discovery unauthorized (401)")
            child.poll.return_value = 1
            c.popen.return_value = child

            def refresh(*args, **kwargs):
                self.assertTrue(c.runner.is_running("conversation"))
                self.assertTrue(c.runner.cancel("conversation", reset_session=False))

            with patch("wechat_codex_multi.codex_usage._refresh_codex_auth", side_effect=refresh):
                with self.assertRaises(CodexCancelled):
                    c.runner.run("conversation", "cancel auth retry")
            c.popen.assert_called_once()
            c.terminate.assert_not_called()
            self.assertEqual(c.state.get_session("conversation", c.cwd)["codexThreadId"], "new-thread")
            c.popen.return_value = c.process()
            self.assertEqual(c.runner.run("conversation", "next task"), "ok")

    def test_launch_failure_does_not_leave_an_active_execution(self):
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent), self.case(agent) as c:
                c.popen.side_effect = OSError("fixture launch failure")
                with self.assertRaisesRegex(OSError, "fixture launch failure"):
                    c.runner.run("conversation", "failed launch")
                self.assertFalse(c.runner.is_running("conversation"))
                self.assertFalse(c.runner.cancel("conversation", reset_session=False))
                c.popen.side_effect = None
                c.popen.return_value = c.process()
                self.assertEqual(c.runner.run("conversation", "next task"), "ok")


if __name__ == "__main__":
    unittest.main()
