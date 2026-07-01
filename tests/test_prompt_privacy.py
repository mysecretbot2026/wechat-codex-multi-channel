import subprocess
import sys
import unittest
from pathlib import Path

from wechat_codex_multi.claude_cli import ClaudeCliRunner
from wechat_codex_multi.codex_app_server import CodexAppServerRunner
from wechat_codex_multi.codex_cli import CodexCliRunner


LEAK_TERMS = ("微信", "wechat", "weixin")


class FakeState:
    state_dir = Path("/tmp")


def assert_no_channel_terms(testcase, text):
    lowered = str(text).lower()
    for term in LEAK_TERMS:
        testcase.assertNotIn(term, lowered)


class PromptPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "codex": {"bin": "codex", "workingDirectory": "/tmp", "timeoutMs": 1000, "extraPrompt": ""},
            "claude": {"bin": "claude", "timeoutMs": 1000, "extraPrompt": ""},
            "media": {"generators": []},
        }

    def test_codex_exec_prompts_do_not_name_channel(self):
        runner = CodexCliRunner(self.config, FakeState())

        assert_no_channel_terms(self, runner._build_prompt("hello", True, []))
        assert_no_channel_terms(self, runner._build_prompt("hello", False, []))

    def test_codex_app_server_instructions_do_not_name_channel(self):
        runner = CodexAppServerRunner(self.config, FakeState())

        assert_no_channel_terms(self, runner._instructions())

    def test_claude_system_prompt_does_not_name_channel(self):
        runner = ClaudeCliRunner(self.config, FakeState())

        assert_no_channel_terms(self, runner._system_prompt())

    def test_send_media_skill_does_not_name_channel(self):
        text = Path("skills/send-media/SKILL.md").read_text(encoding="utf-8")

        assert_no_channel_terms(self, text)

    def test_local_agent_tools_help_does_not_name_channel(self):
        root = Path(__file__).resolve().parent.parent
        completed = subprocess.run(
            [sys.executable, "-m", "local_agent_tools", "--help"],
            cwd="/tmp",
            env={"PYTHONPATH": str(root)},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )

        self.assertEqual(completed.returncode, 0)
        assert_no_channel_terms(self, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
