import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from wechat_codex_multi.claude_usage import (
    format_claude_usage,
    format_claude_usage_all,
    read_claude_auth_status,
    read_claude_usage,
)


class ClaudeUsageTests(unittest.TestCase):
    def test_read_claude_usage_parses_slash_command_and_stats_cache(self):
        with tempfile.TemporaryDirectory() as claude_dir:
            stats = {
                "lastComputedDate": "2026-05-07",
                "totalSessions": 2,
                "totalMessages": 5,
                "modelUsage": {
                    "claude-sonnet-4-6": {
                        "inputTokens": 10,
                        "outputTokens": 3,
                        "cacheReadInputTokens": 7,
                        "cacheCreationInputTokens": 2,
                        "webSearchRequests": 1,
                        "costUSD": 0.12,
                    }
                },
            }
            Path(claude_dir, "stats-cache.json").write_text(json.dumps(stats), encoding="utf-8")
            json_result = Mock(returncode=0, stdout='{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"firstParty"}', stderr="")
            text_result = Mock(
                returncode=0,
                stdout="Login method: Claude Pro account\nOrganization: user@example.com's Organization\nEmail: user@example.com\n",
                stderr="",
            )

            with patch("wechat_codex_multi.claude_usage.subprocess.run", side_effect=[json_result, text_result]) as run:
                usage = read_claude_usage("claude-dev", claude_config_dir=claude_dir, permission_mode="bypassPermissions")

            args = run.call_args_list[0].args[0]
            self.assertEqual(args[:3], ["claude-dev", "auth", "status"])
            self.assertEqual(run.call_args_list[0].kwargs["env"]["CLAUDE_CONFIG_DIR"], str(Path(claude_dir).resolve()))
            self.assertTrue(usage["auth"]["loggedIn"])
            self.assertEqual(usage["auth"]["email"], "user@example.com")
            self.assertEqual(usage["stats"]["modelUsage"]["claude-sonnet-4-6"]["inputTokens"], 10)

    def test_read_claude_auth_status_parses_text_email_when_json_omits_it(self):
        json_result = Mock(returncode=0, stdout='{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"firstParty","email":null}', stderr="")
        text_result = Mock(
            returncode=0,
            stdout="Login method: Claude Pro account\nOrganization: user@example.com's Organization\nEmail: user@example.com\n",
            stderr="",
        )

        with patch("wechat_codex_multi.claude_usage.subprocess.run", side_effect=[json_result, text_result]):
            status = read_claude_auth_status("claude")

        self.assertTrue(status["loggedIn"])
        self.assertEqual(status["email"], "user@example.com")
        self.assertEqual(status["orgName"], "user@example.com's Organization")

    def test_format_claude_usage_includes_subscription_and_model_tokens(self):
        text = format_claude_usage(
            {
                "subscription": {"text": "subscription ok"},
                "auth": {
                    "loggedIn": True,
                    "email": "user@example.com",
                    "authMethod": "Claude Pro account",
                    "apiProvider": "firstParty",
                },
                "stats": {
                    "exists": True,
                    "lastComputedDate": "2026-05-07",
                    "totalSessions": 2,
                    "totalMessages": 5,
                    "modelUsage": {"sonnet": {"inputTokens": 10, "outputTokens": 4}},
                },
            },
            {"name": "main", "claudeConfigDir": ""},
        )

        self.assertIn("Claude 用量", text)
        self.assertIn("登录状态：已登录", text)
        self.assertIn("邮箱：user@example.com", text)
        self.assertIn("sonnet", text)
        self.assertIn("input=10", text)

    def test_format_claude_usage_all_reports_per_account_errors(self):
        text = format_claude_usage_all(
            [
                {"account": {"name": "main", "claudeConfigDir": ""}, "error": "not logged in"},
            ]
        )

        self.assertIn("[main]", text)
        self.assertIn("读取失败：not logged in", text)


if __name__ == "__main__":
    unittest.main()
