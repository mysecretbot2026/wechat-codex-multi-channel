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
            session = {"codexThreadId": ""}

            thread_id = runner._ensure_thread(
                server, context, "conversation-1", session, tmp, "", "", "main", version, instructions
            )

            self.assertEqual(thread_id, "thread-new")
            method, params, _timeout = server.requests[0]
            self.assertEqual(method, "thread/start")
            self.assertEqual(params["baseInstructions"], instructions)
            self.assertEqual(state.updates[-1][1]["codexAppServerPromptVersion"], version)


if __name__ == "__main__":
    unittest.main()
