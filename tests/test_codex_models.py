import unittest
from unittest.mock import patch

from wechat_codex_multi.codex_models import find_model_option, format_model_option, model_options


class CodexModelTests(unittest.TestCase):
    def test_find_model_option_by_index_and_key(self):
        options = [
            {"model": "gpt-5.5", "reasoningEffort": "medium", "label": "GPT-5.5"},
            {"model": "gpt-5.5", "reasoningEffort": "high", "label": "GPT-5.5"},
        ]

        self.assertEqual(find_model_option(options, "2")["reasoningEffort"], "high")
        self.assertEqual(find_model_option(options, "gpt-5.5:medium")["reasoningEffort"], "medium")

    def test_format_model_option_includes_label(self):
        self.assertEqual(
            format_model_option({"model": "gpt-5.5", "reasoningEffort": "high", "label": "GPT-5.5"}),
            "gpt-5.5:high (GPT-5.5)",
        )

    def test_default_model_options_match_supported_matrix(self):
        with patch("wechat_codex_multi.codex_models.discover_model_options") as discover:
            options = model_options({"codex": {"modelOptions": []}})

        discover.assert_not_called()
        self.assertEqual(len(options), 24)
        self.assertEqual(format_model_option(options[0]), "gpt-5.5:low")
        self.assertEqual(format_model_option(options[3]), "gpt-5.5:xhigh")
        self.assertEqual(format_model_option(options[-1]), "codex-auto-review:xhigh")


if __name__ == "__main__":
    unittest.main()
