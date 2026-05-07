import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from wechat_codex_multi.claude_usage import (
    format_claude_usage,
    format_claude_usage_all,
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
            process = Mock()
            stdout = Mock()
            stderr = Mock()
            stdout.fileno.return_value = 1
            stderr.fileno.return_value = 2
            process.stdout = stdout
            process.stderr = stderr
            process.poll.return_value = None

            with patch("wechat_codex_multi.claude_usage.subprocess.Popen", return_value=process) as popen:
                with patch("wechat_codex_multi.claude_usage.select.select") as select:
                    with patch("wechat_codex_multi.claude_usage.os.read") as read:
                        select.side_effect = [([stdout], [], []), ([stdout], [], [])]
                        read.side_effect = [
                            b'{"type":"assistant","message":{"content":[{"type":"text","text":"draft"}]}}\n',
                            b'{"type":"result","is_error":false,"result":"subscription ok"}\n',
                        ]
                        usage = read_claude_usage("claude-dev", claude_config_dir=claude_dir, permission_mode="bypassPermissions")

            args = popen.call_args.args[0]
            self.assertIn("--verbose", args)
            self.assertEqual(args[-1], "/usage")
            self.assertEqual(popen.call_args.kwargs["env"]["CLAUDE_CONFIG_DIR"], str(Path(claude_dir).resolve()))
            self.assertEqual(usage["subscription"]["text"], "subscription ok")
            self.assertEqual(usage["stats"]["modelUsage"]["claude-sonnet-4-6"]["inputTokens"], 10)

    def test_format_claude_usage_includes_subscription_and_model_tokens(self):
        text = format_claude_usage(
            {
                "subscription": {"text": "subscription ok"},
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
        self.assertIn("订阅状态：subscription ok", text)
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
