import unittest
from unittest.mock import Mock, patch

from wechat_codex_multi.codex_usage import format_codex_usage, read_codex_usage


class CodexUsageTests(unittest.TestCase):
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
                "rateLimits": {
                    "planType": "plus",
                    "primary": {"usedPercent": 27, "windowDurationMins": 300, "resetsAt": 1777348464},
                    "secondary": {"usedPercent": 31, "windowDurationMins": 10080, "resetsAt": 1777775193},
                    "credits": {"hasCredits": False, "balance": "0", "unlimited": False},
                }
            }
        )

        self.assertIn("套餐：plus", text)
        self.assertIn("5 小时窗口：已用 27%", text)
        self.assertIn("周窗口：已用 31%", text)
        self.assertIn("credits：hasCredits=false balance=0 unlimited=false", text)


if __name__ == "__main__":
    unittest.main()
