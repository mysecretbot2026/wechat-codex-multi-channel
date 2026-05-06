import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from wechat_codex_multi.codex_usage import format_codex_usage, format_codex_usage_all, read_codex_usage


class CodexUsageTests(unittest.TestCase):
    def test_read_codex_usage_uses_codex_home_auth_directly(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "user_id": "user-1234567890",
                        "account_id": "account-abcdef",
                        "email": "user@example.com",
                        "plan_type": "plus",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 42,
                                "limit_window_seconds": 18000,
                                "reset_at": 1777348464,
                            },
                            "secondary_window": {
                                "used_percent": 12,
                                "limit_window_seconds": 604800,
                                "reset_at": 1777775193,
                            },
                        },
                        "credits": {"has_credits": False, "balance": "0", "unlimited": False},
                    }
                ).encode("utf-8")

        with tempfile.TemporaryDirectory() as codex_home:
            with open(f"{codex_home}/auth.json", "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "auth_mode": "chatgpt",
                        "tokens": {"access_token": "token-from-selected-home"},
                    },
                    handle,
                )

            with patch("wechat_codex_multi.codex_usage.urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
                with patch("wechat_codex_multi.codex_usage.subprocess.Popen") as popen:
                    usage = read_codex_usage("codex", timeout_s=1, codex_home=codex_home)

        request = urlopen.call_args.args[0]
        self.assertEqual(request.headers["Authorization"], "Bearer token-from-selected-home")
        self.assertEqual(usage["account"]["email"], "user@example.com")
        self.assertEqual(usage["rateLimits"]["primary"]["usedPercent"], 42)
        self.assertEqual(usage["rateLimits"]["primary"]["windowDurationMins"], 300)
        popen.assert_not_called()

    def test_read_codex_usage_parses_rate_limits_response(self):
        class FakeStream:
            def __init__(self, lines=None):
                self.lines = list(lines or [])

            def readline(self):
                return self.lines.pop(0) if self.lines else ""

        process = Mock()
        process.stdin = Mock()
        process.stdout = FakeStream(
            [
                '{"id":1,"result":{"ok":true}}\n',
                '{"id":2,"result":{"rateLimits":{"planType":"plus","primary":{"usedPercent":27}}}}\n',
            ]
        )
        process.stderr = FakeStream()
        process.returncode = 0
        process.poll.return_value = None

        with patch("wechat_codex_multi.codex_usage.subprocess.Popen", return_value=process):
            with patch("wechat_codex_multi.codex_usage.select.select", side_effect=[([process.stdout], [], [])] * 2):
                usage = read_codex_usage("codex", timeout_s=1, codex_home="/tmp/codex-home")

        self.assertEqual(usage["rateLimits"]["planType"], "plus")
        self.assertEqual(usage["rateLimits"]["primary"]["usedPercent"], 27)
        self.assertEqual(process.stdin.write.call_count, 1)

    def test_format_codex_usage(self):
        text = format_codex_usage(
            {
                "account": {"email": "user@example.com", "accountId": "account-abcdef"},
                "rateLimits": {
                    "planType": "plus",
                    "primary": {"usedPercent": 27, "windowDurationMins": 300, "resetsAt": 1777348464},
                    "secondary": {"usedPercent": 31, "windowDurationMins": 10080, "resetsAt": 1777775193},
                    "credits": {"hasCredits": False, "balance": "0", "unlimited": False},
                }
            }
        )

        self.assertIn("登录账号：user@example.com (account-abcdef)", text)
        self.assertIn("套餐：plus", text)
        self.assertIn("5 小时窗口：已用 27%", text)
        self.assertIn("周窗口：已用 31%", text)
        self.assertIn("credits：hasCredits=false balance=0 unlimited=false", text)

    def test_format_codex_usage_all(self):
        text = format_codex_usage_all(
            [
                {
                    "account": {"name": "main", "codexHome": "/tmp/main"},
                    "usage": {
                        "rateLimits": {
                            "planType": "plus",
                            "primary": {"usedPercent": 27},
                            "secondary": {"usedPercent": 31},
                        }
                    },
                },
                {
                    "account": {"name": "backup", "codexHome": "/tmp/backup"},
                    "error": "not logged in",
                },
            ]
        )

        self.assertIn("Codex 全部账号用量：", text)
        self.assertIn("[main]", text)
        self.assertIn("codexHome: /tmp/main", text)
        self.assertIn("5 小时窗口：已用 27%", text)
        self.assertIn("[backup]", text)
        self.assertIn("读取失败：not logged in", text)


if __name__ == "__main__":
    unittest.main()
