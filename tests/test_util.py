import unittest

from wechat_codex_multi.util import markdown_to_plain_text


class UtilTests(unittest.TestCase):
    def test_markdown_to_plain_text_removes_common_markup(self):
        text = "\n".join(
            [
                "# 标题",
                "**重点** 和 `code`",
                "[链接](https://example.test)",
                "- item",
                "```python",
                "print('ok')",
                "```",
            ]
        )

        plain = markdown_to_plain_text(text)

        self.assertIn("标题", plain)
        self.assertIn("重点 和 code", plain)
        self.assertIn("链接", plain)
        self.assertIn("- item", plain)
        self.assertIn("print('ok')", plain)
        self.assertNotIn("```", plain)
        self.assertNotIn("**", plain)


if __name__ == "__main__":
    unittest.main()
