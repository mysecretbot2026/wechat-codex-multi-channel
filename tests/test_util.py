import unittest

from wechat_codex_multi.util import markdown_to_plain_text


class UtilTests(unittest.TestCase):
    def test_markdown_to_plain_text_removes_common_markup(self):
        text = "\n".join(
            [
                "# 标题",
                "**重点** 和 _强调_ 和 `code`",
                "[链接](https://example.test)",
                "- item",
                "```python",
                "print('ok')",
                "```",
            ]
        )

        plain = markdown_to_plain_text(text)

        self.assertIn("标题", plain)
        self.assertIn("重点 和 强调 和 code", plain)
        self.assertIn("链接", plain)
        self.assertIn("- item", plain)
        self.assertIn("print('ok')", plain)
        self.assertNotIn("```", plain)
        self.assertNotIn("**", plain)

    def test_markdown_to_plain_text_preserves_underscores_in_names(self):
        text = "目录: xx_gg 和 yy_zz\n路径: /tmp/project_a/xx_gg/file_name.txt"

        plain = markdown_to_plain_text(text)

        self.assertIn("xx_gg", plain)
        self.assertIn("yy_zz", plain)
        self.assertIn("/tmp/project_a/xx_gg/file_name.txt", plain)


if __name__ == "__main__":
    unittest.main()
