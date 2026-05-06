import unittest
import subprocess
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

    def test_model_options_discovers_models_when_not_configured(self):
        with patch("wechat_codex_multi.codex_models.discover_model_options") as discover:
            discover.return_value = [{"model": "gpt-live", "reasoningEffort": "medium"}]
            options = model_options({"codex": {"bin": "codex-dev", "modelOptions": []}})

        discover.assert_called_once_with("codex-dev", timeout_s=30, codex_home="")
        self.assertEqual(options, [{"model": "gpt-live", "reasoningEffort": "medium"}])

    def test_model_options_uses_configured_discovery_timeout_and_codex_home(self):
        config = {
            "codex": {
                "bin": "codex-dev",
                "modelOptions": [],
                "modelDiscoveryTimeoutSeconds": 45,
                "defaultAccount": "main",
                "accounts": [{"name": "main", "codexHome": "/tmp/codex-home"}],
            }
        }
        with patch("wechat_codex_multi.codex_models.discover_model_options") as discover:
            discover.return_value = [{"model": "gpt-live", "reasoningEffort": "medium"}]
            options = model_options(config)

        discover.assert_called_once_with("codex-dev", timeout_s=45, codex_home="/tmp/codex-home")
        self.assertEqual(options, [{"model": "gpt-live", "reasoningEffort": "medium"}])

    def test_model_options_falls_back_to_defaults_when_discovery_times_out(self):
        with patch("wechat_codex_multi.codex_models.discover_model_options") as discover:
            discover.side_effect = subprocess.TimeoutExpired(["codex", "debug", "models"], 30)
            options = model_options({"codex": {"bin": "codex-dev", "modelOptions": []}})

        self.assertTrue(any(option["model"] == "gpt-5.5" for option in options))

    def test_configured_model_options_skip_discovery(self):
        configured = [{"model": "gpt-fixed", "reasoningEffort": "high"}]
        with patch("wechat_codex_multi.codex_models.discover_model_options") as discover:
            options = model_options({"codex": {"modelOptions": configured}})

        discover.assert_not_called()
        self.assertEqual(options, configured)


if __name__ == "__main__":
    unittest.main()
