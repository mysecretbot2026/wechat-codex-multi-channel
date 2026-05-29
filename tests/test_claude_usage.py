import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from wechat_codex_multi.claude_usage import (
    _beautify_claude_usage_text,
    _extract_usage_panel,
    _is_trust_prompt,
    _looks_like_usage_result,
    format_claude_admin_usage,
    format_claude_usage,
    format_claude_usage_all,
    read_claude_auth_status,
    read_claude_admin_usage,
    read_claude_usage,
    read_claude_usage_interactive,
)


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.data).encode("utf-8")


class ClaudeUsageTests(unittest.TestCase):
    def test_read_claude_usage_uses_interactive_pty(self):
        with tempfile.TemporaryDirectory() as claude_dir:
            with patch("wechat_codex_multi.claude_usage.read_claude_usage_interactive") as interactive:
                interactive.return_value = {"source": "interactive-pty", "text": "Claude Usage\n5-hour limit: 10%"}
                usage = read_claude_usage(
                    "claude-dev",
                    timeout_s=30,
                    claude_config_dir=claude_dir,
                    permission_mode="bypassPermissions",
                    cwd="/tmp/work",
                )

            interactive.assert_called_once_with(
                claude_bin="claude-dev",
                timeout_s=30,
                claude_config_dir=claude_dir,
                permission_mode="bypassPermissions",
                cwd="/tmp/work",
            )
            self.assertNotIn("stats", usage)
            self.assertNotIn("auth", usage)
            self.assertEqual(usage["interactive"]["text"], "Claude Usage\n5-hour limit: 10%")

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

    def test_format_claude_usage_includes_interactive_output(self):
        text = format_claude_usage(
            {
                "interactive": {
                    "source": "interactive-pty",
                    "durationSeconds": 3.2,
                    "text": "Claude Usage\n5-hour limit: 10%",
                }
            },
            {"name": "main", "claudeConfigDir": ""},
        )

        self.assertIn("Claude 用量", text)
        self.assertIn("Claude TUI /usage", text)
        self.assertIn("5-hour limit: 10%", text)

    def test_extract_usage_panel_cleans_terminal_text(self):
        text = _extract_usage_panel("\x1b[2JWelcome\n> /usage\nClaude Usage\nTokens: 123\nCost: $0.12")

        self.assertIn("Claude Usage", text)
        self.assertIn("Tokens: 123", text)
        self.assertNotIn("> /usage", text)
        self.assertNotIn("\x1b", text)

    def test_beautify_claude_usage_text_formats_collapsed_tui_output(self):
        text = _beautify_claude_usage_text(
            "────────────────────\n"
            "Status   Config Usage Stats\n"
            "Session\n"
            "Totalcost:$0.0000\n"
            "Totalduration(API):0s\n"
            "Totalduration(wall):9s\n"
            "Totalcodechanges:0linesadded,0linesremoved\n"
            "Usage:0input,0output,0cacheread,0cachewrite\n"
            "Currentsession\n"
            "2%used\n"
            "Resets7pm(Asia/Shanghai)\n"
            "Currentweek(allmodels)\n"
            "0%used\n"
            "ResetsJun1,8am(Asia/Shanghai)\n"
            "Currentweek(Sonnetonly)\n"
            "0%used\n"
            "Refreshing…\n"
            "Esctocancel\n"
            "Settings  Status   Config   Usage Stats\n"
            "What's contributing to your limits usage?\n"
            "Approximate,basedonlocalsessionsonthismachine\n"
            "Last 24h· these are independent characteristics of your usage, not a breakdown\n"
            "64% of yourusagewasat>150kcontext\n"
            "Longer sessions are more expensive even when cached. /compact mid-task, /clear\n"
            "when swithing to new tasks.\n"
            "Skills,subagents,plugins,andMCPservers\n"
            "Noattributiondatayet·accumulatesasyouuseClaude\n"
            "dtoday·wtoweek\n"
            "█2\n"
            "Usage credits\n"
            "Usage credits are off · /usage-credits to turn them on\n"
        )

        self.assertIn("Session", text)
        self.assertIn("  Total cost: $0.0000", text)
        self.assertIn("  API duration: 0s", text)
        self.assertIn("  Tokens: input 0, output 0, cache read 0, cache write 0", text)
        self.assertIn("Current session", text)
        self.assertIn("█", text)
        self.assertIn("2% used", text)
        self.assertIn("  Resets 7pm (Asia/Shanghai)", text)
        self.assertIn("Current week (all models)", text)
        self.assertIn("  Resets Jun 1, 8am (Asia/Shanghai)", text)
        self.assertIn("Current week (Sonnet only)", text)
        self.assertNotIn("Settings", text)
        self.assertIn("What's contributing to your limits usage?", text)
        self.assertIn("  Approximate, based on local sessions on this machine", text)
        self.assertIn("Last 24h", text)
        self.assertIn("  These are independent characteristics of your usage, not a breakdown", text)
        self.assertIn("  64% of your usage was at >150k context", text)
        self.assertIn("  when switching to new tasks.", text)
        self.assertIn("Skills, subagents, plugins, and MCP servers", text)
        self.assertIn("  No attribution data yet · accumulates as you use Claude", text)
        self.assertIn("Usage credits", text)
        self.assertIn("  Usage credits are off · /usage-credits to turn them on", text)
        self.assertNotIn("dtoday", text)
        self.assertNotIn("█2", text)
        self.assertNotIn("Refreshing", text)
        self.assertNotIn("Esc", text)

    def test_usage_detection_rejects_trust_prompt(self):
        trust = "Quick safety check: Is this a project you trust?\n1. Yes, I trust this folder"

        self.assertTrue(_is_trust_prompt(trust))
        self.assertFalse(_looks_like_usage_result(trust))

    @unittest.skipIf(os.name != "posix", "PTY test requires POSIX")
    def test_interactive_usage_confirms_trust_prompt_before_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "claude-fake"
            script.write_text(
                "#!/bin/sh\n"
                "printf 'Quick safety check: trust this folder?\\r\\n1. Yes, I trust this folder\\r\\n'\n"
                "IFS= read trust\n"
                "printf '\\033[2JWelcome to Claude\\r\\n> '\n"
                "IFS= read cmd\n"
                "printf '\\033[2JClaude Usage\\r\\nTokens: 123\\r\\nCost: $0.12\\r\\n'\n"
                "sleep 60\n",
                encoding="utf-8",
            )
            script.chmod(0o755)

            result = read_claude_usage_interactive(
                str(script),
                timeout_s=5,
                stable_s=0.1,
                post_trust_delay_s=0.1,
                cwd=tmp,
            )

        self.assertFalse(result.get("timedOut"))
        self.assertNotIn("trust this folder", result.get("text", "").lower())
        self.assertIn("Claude Usage", result.get("text", ""))
        self.assertIn("Tokens: 123", result.get("text", ""))

    def test_format_claude_usage_all_reports_per_account_errors(self):
        text = format_claude_usage_all(
            [
                {"account": {"name": "main", "claudeConfigDir": ""}, "error": "not logged in"},
            ]
        )

        self.assertIn("[main]", text)
        self.assertIn("读取失败：not logged in", text)

    def test_read_claude_admin_usage_uses_official_usage_and_cost_reports(self):
        responses = [
            FakeResponse({"id": "org-1", "name": "Org One", "type": "organization"}),
            FakeResponse(
                {
                    "data": [
                        {
                            "starting_at": "2026-05-19T00:00:00Z",
                            "ending_at": "2026-05-20T00:00:00Z",
                            "results": [
                                {
                                    "model": "claude-sonnet-4-6",
                                    "uncached_input_tokens": 10,
                                    "cache_read_input_tokens": 20,
                                    "cache_creation": {
                                        "ephemeral_5m_input_tokens": 30,
                                        "ephemeral_1h_input_tokens": 40,
                                    },
                                    "output_tokens": 50,
                                    "server_tool_use": {"web_search_requests": 2},
                                }
                            ],
                        }
                    ],
                    "has_more": False,
                }
            ),
            FakeResponse(
                {
                    "data": [
                        {
                            "starting_at": "2026-05-19T00:00:00Z",
                            "ending_at": "2026-05-20T00:00:00Z",
                            "results": [{"amount": "123.45", "currency": "USD"}],
                        }
                    ],
                    "has_more": False,
                }
            ),
        ]

        with patch("wechat_codex_multi.claude_usage.urllib.request.urlopen", side_effect=responses) as urlopen:
            usage = read_claude_admin_usage(
                api_key="sk-ant-admin-test",
                days=7,
                now=datetime(2026, 5, 25, tzinfo=timezone.utc),
            )

        urls = [call.args[0].full_url for call in urlopen.call_args_list]
        self.assertIn("/v1/organizations/me", urls[0])
        self.assertIn("/v1/organizations/usage_report/messages", urls[1])
        self.assertIn("group_by%5B%5D=model", urls[1])
        self.assertIn("/v1/organizations/cost_report", urls[2])
        self.assertEqual(usage["summary"]["inputTokens"], 100)
        self.assertEqual(usage["summary"]["outputTokens"], 50)
        self.assertEqual(usage["summary"]["webSearchRequests"], 2)
        self.assertEqual(str(usage["costSummary"]["amount"]), "1.2345")

    def test_format_claude_admin_usage_includes_summary(self):
        text = format_claude_admin_usage(
            {
                "configured": True,
                "source": "env:ANTHROPIC_ADMIN_KEY",
                "days": 7,
                "requestedDays": 7,
                "startingAt": "2026-05-19T00:00:00Z",
                "endingAt": "2026-05-26T00:00:00Z",
                "organization": {"name": "Org One"},
                "summary": {
                    "inputTokens": 100,
                    "outputTokens": 50,
                    "totalTokens": 150,
                    "cacheReadInputTokens": 20,
                    "cacheCreation5mInputTokens": 30,
                    "cacheCreation1hInputTokens": 40,
                    "models": {"claude-sonnet-4-6": {"inputTokens": 100, "outputTokens": 50, "totalTokens": 150}},
                },
                "costSummary": {"amount": Decimal("1.2345"), "currency": "USD"},
            }
        )

        self.assertIn("Claude Admin API 用量", text)
        self.assertIn("组织：Org One", text)
        self.assertIn("tokens: input=100 output=50 total=150", text)
        self.assertIn("cost: $1.2345 USD", text)

    def test_read_claude_admin_usage_returns_error_when_usage_api_fails(self):
        responses = [FakeResponse({"id": "org_1", "name": "Org One"}), RuntimeError("boom")]

        with patch("wechat_codex_multi.claude_usage.urllib.request.urlopen", side_effect=responses):
            usage = read_claude_admin_usage(
                api_key="sk-ant-admin-test",
                days=7,
                now=datetime(2026, 5, 25, tzinfo=timezone.utc),
            )

        text = format_claude_admin_usage(usage)
        self.assertEqual(usage["error"], "boom")
        self.assertIn("Admin API 读取失败：boom", text)


if __name__ == "__main__":
    unittest.main()
