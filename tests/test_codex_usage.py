import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from wechat_codex_multi.codex_usage import format_codex_usage, format_codex_usage_all, read_codex_usage


class CodexUsageTests(unittest.TestCase):
    def setUp(self):
        # These tests mock the child server, so binary lookup is a separate unit.
        resolver = patch("wechat_codex_multi.codex_usage.resolve_codex_bin", side_effect=lambda value, **_kwargs: value)
        resolver.start()
        self.addCleanup(resolver.stop)

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
                with patch("wechat_codex_multi.codex_app_server.subprocess.Popen") as popen:
                    usage = read_codex_usage("codex", timeout_s=1, codex_home=codex_home)

        request = urlopen.call_args.args[0]
        self.assertEqual(request.headers["Authorization"], "Bearer token-from-selected-home")
        self.assertEqual(usage["account"]["email"], "user@example.com")
        self.assertEqual(usage["rateLimits"]["primary"]["usedPercent"], 42)
        self.assertEqual(usage["rateLimits"]["primary"]["windowDurationMins"], 300)
        popen.assert_not_called()

    def test_read_codex_usage_parses_rate_limits_response(self):
        server = Mock()
        server.request.side_effect = [
            {"account": {"type": "chatgpt", "email": "user@example.com", "planType": "plus"}},
            {"rateLimits": {"planType": "plus", "primary": {"usedPercent": 27}}},
        ]
        with patch("wechat_codex_multi.codex_usage._read_codex_usage_backend", side_effect=RuntimeError("no auth.json")):
            with patch("wechat_codex_multi.codex_usage.AppServerProcess", return_value=server) as factory:
                usage = read_codex_usage("/selected/codex", timeout_s=1, codex_home="/tmp/codex-home")

        self.assertEqual(usage["rateLimits"]["planType"], "plus")
        self.assertEqual(usage["rateLimits"]["primary"]["usedPercent"], 27)
        self.assertEqual(usage["account"]["email"], "user@example.com")
        factory.assert_called_once_with("/selected/codex", codex_home=str(Path("/tmp/codex-home").resolve()))
        self.assertEqual([c.args[0] for c in server.request.call_args_list], ["account/read", "account/rateLimits/read"])
        server.close.assert_called_once()

    def test_desktop_usage_bypasses_disk_auth_and_uses_fresh_process_each_time(self):
        with tempfile.TemporaryDirectory() as home:
            servers = [Mock(), Mock()]
            for index, server in enumerate(servers):
                server.request.side_effect = [
                    {"account": {"type": "chatgpt", "email": f"user-{index}@example.com", "planType": "pro"}},
                    {"rateLimits": {"primary": {"usedPercent": index, "windowDurationMins": 10080}}},
                ]
            with patch("wechat_codex_multi.codex_usage.AppServerProcess", side_effect=servers) as factory:
                with patch("wechat_codex_multi.codex_usage._read_codex_usage_backend") as backend:
                    results = [read_codex_usage("/desktop/codex", codex_home=home, prefer_app_server=True) for _ in servers]

        backend.assert_not_called()
        self.assertEqual(factory.call_count, 2)
        self.assertNotEqual(results[0]["account"]["email"], results[1]["account"]["email"])
        self.assertIn("套餐：pro", format_codex_usage(results[1]))
        for server in servers:
            server.close.assert_called_once()
            self.assertEqual([c.args[0] for c in server.request.call_args_list], ["account/read", "account/rateLimits/read"])
            self.assertEqual(server.request.call_args_list[0].args[1], {"refreshToken": False})

    def test_app_server_usage_errors_close_process_without_disk_fallback(self):
        cases = [
            ([{"account": None}], "未登录"),
            ([{"account": {"type": "apiKey"}}], "API Key"),
            ([TimeoutError("account timeout")], "account timeout"),
            ([{"account": {"type": "chatgpt"}}, RuntimeError("rate limit read failed")], "rate limit read failed"),
        ]
        for responses, error in cases:
            with self.subTest(error=error):
                server = Mock()
                server.request.side_effect = responses
                with patch("wechat_codex_multi.codex_usage.AppServerProcess", return_value=server):
                    with patch("wechat_codex_multi.codex_usage._read_codex_usage_backend") as backend:
                        with self.assertRaisesRegex(Exception, error):
                            read_codex_usage(prefer_app_server=True)
                backend.assert_not_called()
                server.close.assert_called_once()

    def test_format_pro_weekly_quota_in_either_slot(self):
        for slot in ("primary", "secondary"):
            with self.subTest(slot=slot):
                text = format_codex_usage({"rateLimits": {
                    "planType": "pro", slot: {"usedPercent": 42, "windowDurationMins": 10080},
                }})
                self.assertIn("5 小时限制：不适用（Pro）", text)
                self.assertIn("周窗口：已用 42%", text)
                self.assertNotIn("5 小时窗口", text)
                self.assertNotIn("无数据", text)

    def test_format_pro_ignores_retired_five_hour_slot_but_keeps_weekly_quota(self):
        text = format_codex_usage({"rateLimits": {
            "planType": "pro",
            "primary": {"usedPercent": 99, "windowDurationMins": 300},
            "secondary": {"usedPercent": 12, "windowDurationMins": 10080},
        }})
        self.assertIn("周窗口：已用 12%", text)
        self.assertNotIn("99%", text)
        self.assertNotIn("5 小时窗口", text)

    def test_format_windows_use_actual_duration_instead_of_slot(self):
        for duration, label in ((15, "15 分钟窗口"), (60, "1 小时窗口"), (1440, "1 天窗口"), (10080, "周窗口")):
            with self.subTest(duration=duration):
                text = format_codex_usage({"rateLimits": {
                    "planType": "plus", "primary": {"usedPercent": 0, "windowDurationMins": duration},
                }})
                self.assertIn(f"{label}：已用 0%", text)
                self.assertNotIn("5 小时窗口", text)

    def test_format_missing_or_invalid_fields_does_not_fabricate_usage_or_crash(self):
        self.assertIn("用量窗口：无数据", format_codex_usage({"rateLimits": {"planType": "pro"}}))
        text = format_codex_usage({"rateLimits": {
            "primary": {"usedPercent": None, "windowDurationMins": "invalid", "resetsAt": "invalid"},
            "secondary": {"usedPercent": "NaN", "windowDurationMins": 60, "resetsAt": 10**100},
        }})
        self.assertIn("主窗口：已用比例无数据", text)
        self.assertIn("1 小时窗口：已用比例无数据", text)
        self.assertIn("重置时间：未知", text)
        self.assertNotIn("None%", text)
        self.assertNotIn("已用 0%", text)

    def test_format_selects_codex_bucket_and_reads_plan_from_account(self):
        text = format_codex_usage({
            "account": {"email": "desktop@example.com", "planType": "pro"},
            "rateLimits": {"limitId": "other", "planType": "plus", "primary": {"usedPercent": 99}},
            "rateLimitsByLimitId": {"codex": {"primary": {"usedPercent": 23, "windowDurationMins": 10080}}},
        })
        self.assertIn("登录账号：desktop@example.com", text)
        self.assertIn("套餐：pro", text)
        self.assertIn("周窗口：已用 23%", text)
        self.assertNotIn("99%", text)

    def test_format_all_handles_plus_and_pro_without_shared_window_assumptions(self):
        text = format_codex_usage_all([
            {"account": {"name": "plus"}, "usage": {"rateLimits": {
                "planType": "plus", "primary": {"usedPercent": 8, "windowDurationMins": 300},
            }}},
            {"account": {"name": "pro"}, "usage": {"rateLimits": {
                "planType": "pro", "primary": {"usedPercent": 32, "windowDurationMins": 10080},
            }}},
        ])
        plus, pro = text.split("[pro]")
        self.assertIn("5 小时窗口：已用 8%", plus)
        self.assertIn("5 小时限制：不适用（Pro）", pro)
        self.assertIn("周窗口：已用 32%", pro)
        self.assertNotIn("5 小时窗口", pro)

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
