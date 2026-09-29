import tempfile
import unittest

from wechat_codex_multi.codex_app_server import CodexAppServerRunner


class FakeState:
    def __init__(self):
        self.updates = []
        self.reset_calls = []

    def update_session(self, conversation_key, **updates):
        self.updates.append((conversation_key, updates))

    def reset_session(self, conversation_key):
        self.reset_calls.append(conversation_key)


class FakeServer:
    def __init__(self):
        self.requests = []
        self.registered = []
        self.unregistered = []

    def request(self, method, params=None, timeout_s=120):
        self.requests.append((method, params or {}, timeout_s))
        if method == "thread/start":
            return {"thread": {"id": "thread-new"}}
        return {}

    def register_context(self, context):
        self.registered.append(context)

    def unregister_context(self, context):
        self.unregistered.append(context)


class FailingResumeServer(FakeServer):
    def request(self, method, params=None, timeout_s=120):
        if method == "thread/resume":
            self.requests.append((method, params or {}, timeout_s))
            raise RuntimeError("resume unavailable")
        return super().request(method, params, timeout_s)


class MissingPendingThreadServer(FakeServer):
    def request(self, method, params=None, timeout_s=120):
        if method == "thread/resume":
            self.requests.append((method, params or {}, timeout_s))
            raise RuntimeError("no rollout found for thread id old-pending")
        return super().request(method, params, timeout_s)


def make_config(tmp):
    return {
        "codex": {
            "bin": "codex",
            "workingDirectory": tmp,
            "timeoutMs": 1000,
            "bypassApprovalsAndSandbox": True,
        },
        "media": {"generators": []},
    }


class CodexAppServerPromptVersionTests(unittest.TestCase):
    def test_resume_with_current_prompt_version_omits_base_instructions(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = CodexAppServerRunner(make_config(tmp), FakeState())
            server = FakeServer()
            context = runner._context("conversation-1", "thread-1")
            instructions = runner._instructions()
            version = runner._prompt_version(instructions)
            session = {"codexThreadId": "thread-1", "codexAppServerPromptVersion": version}

            thread_id = runner._ensure_thread(
                server, context, "conversation-1", session, tmp, "", "", "main", version, instructions
            )

            self.assertEqual(thread_id, "thread-1")
            method, params, _timeout = server.requests[0]
            self.assertEqual(method, "thread/resume")
            self.assertNotIn("baseInstructions", params)
            self.assertNotIn("developerInstructions", params)

    def test_resume_with_old_prompt_version_includes_base_instructions(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = CodexAppServerRunner(make_config(tmp), FakeState())
            server = FakeServer()
            context = runner._context("conversation-1", "thread-1")
            instructions = runner._instructions()
            version = runner._prompt_version(instructions)
            session = {"codexThreadId": "thread-1", "codexAppServerPromptVersion": "old"}

            thread_id = runner._ensure_thread(
                server, context, "conversation-1", session, tmp, "", "", "main", version, instructions
            )

            self.assertEqual(thread_id, "thread-1")
            method, params, _timeout = server.requests[0]
            self.assertEqual(method, "thread/resume")
            self.assertEqual(params["baseInstructions"], instructions)
            self.assertEqual(params["developerInstructions"], "")

    def test_new_thread_records_prompt_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = FakeState()
            runner = CodexAppServerRunner(make_config(tmp), state)
            server = FakeServer()
            context = runner._context("conversation-1", "")
            instructions = runner._instructions()
            version = runner._prompt_version(instructions)
            session = {"codexThreadId": "", "desktopProjectId": "native-project-1"}

            thread_id = runner._ensure_thread(
                server, context, "conversation-1", session, tmp, "", "", "main", version, instructions
            )

            self.assertEqual(thread_id, "thread-new")
            method, params, _timeout = server.requests[0]
            self.assertEqual(method, "thread/start")
            self.assertEqual(params["projectId"], "native-project-1")
            self.assertEqual(params["baseInstructions"], instructions)
            self.assertEqual(state.updates[-1][1]["codexAppServerPromptVersion"], version)

    def test_desktop_resume_failure_keeps_selected_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            config["codex"]["preserveExistingInstructions"] = True
            state = FakeState()
            runner = CodexAppServerRunner(config, state)
            server = FailingResumeServer()
            context = runner._context("conversation-1", "thread-1")
            instructions = runner._instructions()
            version = runner._prompt_version(instructions)
            with self.assertRaisesRegex(RuntimeError, "无法续聊所选桌面会话"):
                runner._ensure_thread(
                    server, context, "conversation-1", {"codexThreadId": "thread-1"},
                    tmp, "", "", "main", version, instructions,
                )
            self.assertEqual([method for method, _, _ in server.requests], ["thread/resume"])
            self.assertEqual(state.reset_calls, [])

    def test_missing_pending_desktop_thread_starts_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            config["codex"]["preserveExistingInstructions"] = True
            state = FakeState()
            runner = CodexAppServerRunner(config, state)
            server = MissingPendingThreadServer()
            context = runner._context("conversation-1", "old-pending")
            instructions = runner._instructions()
            version = runner._prompt_version(instructions)
            result = runner._ensure_thread(
                server, context, "conversation-1",
                {"codexThreadId": "old-pending", "desktopPendingThread": True},
                tmp, "", "", "main", version, instructions,
            )
            self.assertEqual(result, "thread-new")
            self.assertEqual([method for method, _, _ in server.requests], ["thread/resume", "thread/start"])


if __name__ == "__main__":
    unittest.main()
