import json
import subprocess
import unittest
from unittest.mock import patch

from wechat_codex_multi.codex_models import (
    default_model_options,
    discover_model_options,
    find_model_option,
    format_model_option,
    model_options,
    normalize_model_option,
)


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

    def test_normalize_model_option_accepts_cli_defined_reasoning_levels(self):
        self.assertEqual(
            normalize_model_option({"model": "gpt-next", "reasoningEffort": "ultra"}),
            {"model": "gpt-next", "reasoningEffort": "ultra"},
        )

    def test_discovery_preserves_per_model_levels_and_filters_hidden_models(self):
        payload = {
            "models": [
                {
                    "slug": "gpt-5.6-sol",
                    "display_name": "GPT-5.6-Sol",
                    "visibility": "list",
                    "supported_reasoning_levels": [
                        {"effort": "low"},
                        {"effort": "max"},
                        {"effort": "ultra"},
                    ],
                },
                {
                    "slug": "gpt-5.6-luna",
                    "display_name": "GPT-5.6-Luna",
                    "visibility": "list",
                    "supported_reasoning_levels": [{"effort": "low"}, {"effort": "max"}],
                },
                {
                    "slug": "gpt-reserve",
                    "display_name": "GPT-Reserve",
                    "visibility": "hide",
                    "supported_reasoning_levels": [{"effort": "max"}],
                },
            ]
        }
        completed = subprocess.CompletedProcess(
            ["codex", "debug", "models"],
            0,
            stdout=json.dumps(payload),
            stderr="",
        )
        with patch("wechat_codex_multi.codex_models.subprocess.run", return_value=completed):
            options = discover_model_options()

        self.assertEqual(
            [(option["model"], option["reasoningEffort"]) for option in options],
            [
                ("gpt-5.6-sol", "low"),
                ("gpt-5.6-sol", "max"),
                ("gpt-5.6-sol", "ultra"),
                ("gpt-5.6-luna", "low"),
                ("gpt-5.6-luna", "max"),
            ],
        )

    def test_default_options_match_current_per_model_reasoning_levels(self):
        options = default_model_options()
        keys = {(option["model"], option["reasoningEffort"]) for option in options}

        self.assertIn(("gpt-5.6-sol", "ultra"), keys)
        self.assertIn(("gpt-5.6-terra", "ultra"), keys)
        self.assertIn(("gpt-5.6-luna", "max"), keys)
        self.assertNotIn(("gpt-5.6-luna", "ultra"), keys)
        self.assertFalse(any(model in {"gpt-reserve", "codex-auto-review"} for model, _ in keys))

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
