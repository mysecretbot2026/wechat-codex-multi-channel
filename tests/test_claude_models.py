import json
import subprocess
import unittest
from unittest.mock import patch

from wechat_codex_multi.claude_models import (
    claude_model_options,
    clear_claude_model_options_cache,
    discover_claude_help_model_options,
    discover_claude_model_options,
    find_claude_model_option,
    parse_claude_help_effort_levels,
    parse_claude_help_model_names,
    parse_claude_stream_json_model_options,
)


class Completed:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class ClaudeModelTests(unittest.TestCase):
    def setUp(self):
        clear_claude_model_options_cache()

    def test_parse_claude_help_model_names_includes_fable_alias_and_full_name(self):
        help_text = """
          --model <model>  Model for the current session. Provide an alias for the
                           latest model (e.g. 'fable', 'opus', or 'sonnet') or a
                           model's full name (e.g. 'claude-fable-5').
        """

        names = parse_claude_help_model_names(help_text)

        self.assertEqual(names, ["fable", "opus", "sonnet", "claude-fable-5"])

    def test_parse_claude_help_effort_levels_uses_cli_choices(self):
        help_text = """
          --effort <level>  Effort level for the current session
                            (low, medium, high, xhigh, max)
          --model <model>   Model for the current session.
        """

        levels = parse_claude_help_effort_levels(help_text)

        self.assertEqual(levels, ["low", "medium", "high", "xhigh", "max"])

    def test_parse_claude_stream_json_model_options_uses_per_model_efforts(self):
        payload = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "response": {
                    "models": [
                        {
                            "value": "default",
                            "displayName": "Default (recommended)",
                            "description": "Opus 4.8 with 1M context",
                            "supportsEffort": True,
                            "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
                        },
                        {
                            "value": "sonnet",
                            "displayName": "Sonnet",
                            "description": "Sonnet 4.6",
                            "supportsEffort": True,
                            "supportedEffortLevels": ["low", "medium", "high", "max"],
                        },
                        {
                            "value": "haiku",
                            "displayName": "Haiku",
                            "description": "Haiku 4.5",
                        },
                    ]
                },
            },
        }

        options = parse_claude_stream_json_model_options(payload)

        self.assertTrue(any(option["model"] == "default" and option["effort"] == "ultracode" for option in options))
        self.assertTrue(any(option["model"] == "sonnet" and option["effort"] == "max" for option in options))
        self.assertFalse(any(option["model"] == "sonnet" and option["effort"] == "xhigh" for option in options))
        self.assertFalse(any(option["model"] == "sonnet" and option["effort"] == "ultracode" for option in options))
        self.assertTrue(any(option["model"] == "haiku" and option["effort"] == "" for option in options))
        self.assertEqual(options[0]["groupLabel"], "Default (recommended) — Opus 4.8 with 1M context")

    def test_parse_claude_stream_json_model_options_skips_ultracode_when_disabled(self):
        payload = {
            "response": {
                "response": {
                    "models": [
                        {
                            "value": "fable",
                            "displayName": "Fable",
                            "supportsEffort": True,
                            "supportedEffortLevels": ["low", "xhigh"],
                        }
                    ]
                }
            }
        }

        options = parse_claude_stream_json_model_options(payload, include_ultracode=False)

        self.assertEqual([option["effort"] for option in options], ["low", "xhigh"])

    def test_discover_claude_model_options_uses_stream_json_initialize(self):
        payload = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": "init-test",
                "response": {
                    "models": [
                        {
                            "value": "sonnet",
                            "displayName": "Sonnet",
                            "description": "Sonnet 4.6",
                            "supportsEffort": True,
                            "supportedEffortLevels": ["low", "high", "max"],
                        }
                    ]
                },
            },
        }
        with patch("wechat_codex_multi.claude_models.subprocess.run") as run:
            run.return_value = Completed(stdout=json.dumps(payload))
            options = discover_claude_model_options(
                "claude-dev",
                timeout_s=7,
                claude_config_dir="/tmp/claude-home",
                cwd="/tmp",
            )

        run.assert_called_once()
        args = run.call_args.args[0]
        self.assertEqual(args[:6], ["claude-dev", "--output-format", "stream-json", "--input-format", "stream-json", "--verbose"])
        self.assertEqual(run.call_args.kwargs["timeout"], 7)
        self.assertTrue(run.call_args.kwargs["env"]["CLAUDE_CONFIG_DIR"].endswith("/tmp/claude-home"))
        self.assertTrue(run.call_args.kwargs["cwd"].endswith("/tmp"))
        request = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(request["type"], "control_request")
        self.assertEqual(request["request"]["subtype"], "initialize")
        self.assertEqual(options[0]["model"], "sonnet")
        self.assertEqual([option["effort"] for option in options], ["low", "high", "max"])

    def test_discover_claude_help_model_options_uses_help_output(self):
        help_text = """
          --effort <level> (low, medium, high, xhigh, max)
          --model <model> (e.g. 'fable', 'opus', or 'sonnet') (e.g. 'claude-fable-5')
        """
        with patch("wechat_codex_multi.claude_models.subprocess.run") as run:
            run.return_value = Completed(stdout=help_text)
            options = discover_claude_help_model_options("claude-dev", timeout_s=7)

        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["claude-dev", "--help"])
        self.assertEqual(run.call_args.kwargs["timeout"], 7)
        self.assertEqual(
            options[0],
            {
                "model": "fable",
                "effort": "",
                "efforts": ["low", "medium", "high", "xhigh", "max"],
                "effortSource": "cli-help-global",
            },
        )
        self.assertTrue(any(option["model"] == "claude-fable-5" for option in options))
        self.assertFalse(any(option.get("effort") == "max" for option in options))

    def test_discover_claude_help_model_options_accepts_new_cli_effort_values(self):
        help_text = """
          --effort <level> (low, turbo)
          --model <model> (e.g. 'fable')
        """
        with patch("wechat_codex_multi.claude_models.subprocess.run") as run:
            run.return_value = Completed(stdout=help_text)
            options = discover_claude_help_model_options("claude-dev")

        self.assertEqual(
            options,
            [
                {
                    "model": "fable",
                    "effort": "",
                    "efforts": ["low", "turbo"],
                    "effortSource": "cli-help-global",
                },
            ],
        )

    def test_find_claude_model_option_accepts_explicit_discovered_effort(self):
        options = [
            {
                "model": "sonnet",
                "effort": "",
                "efforts": ["low", "high"],
                "effortSource": "cli-help-global",
            }
        ]

        target = find_claude_model_option(options, "sonnet:high")

        self.assertEqual(
            target,
            {
                "model": "sonnet",
                "effort": "high",
                "efforts": ["low", "high"],
                "effortSource": "cli-help-global",
            },
        )

    def test_find_claude_model_option_rejects_undiscovered_effort(self):
        options = [
            {
                "model": "sonnet",
                "effort": "",
                "efforts": ["low", "high"],
                "effortSource": "cli-help-global",
            }
        ]

        self.assertIsNone(find_claude_model_option(options, "sonnet:ultracode"))

    def test_claude_model_options_discovers_when_not_configured(self):
        with patch("wechat_codex_multi.claude_models.discover_claude_model_options") as discover:
            discover.return_value = [{"model": "fable", "effort": "high"}]
            options = claude_model_options(
                {"claude": {"bin": "claude-dev", "modelOptions": [], "modelDiscoveryTimeoutSeconds": 8}},
                claude_config_dir="/tmp/claude-home",
                cwd="/tmp",
            )

        discover.assert_called_once_with(
            "claude-dev",
            timeout_s=8,
            claude_config_dir="/tmp/claude-home",
            cwd="/tmp",
        )
        self.assertEqual(options, [{"model": "fable", "effort": "high"}])

    def test_claude_model_options_falls_back_to_defaults_when_discovery_times_out(self):
        with patch("wechat_codex_multi.claude_models.discover_claude_model_options") as discover:
            discover.side_effect = subprocess.TimeoutExpired(["claude", "--help"], 5)
            options = claude_model_options({"claude": {"bin": "claude-dev", "modelOptions": []}})

        self.assertTrue(any(option["model"] == "fable" for option in options))
        self.assertTrue(any(option["model"] == "sonnet" for option in options))

    def test_configured_claude_model_options_skip_discovery(self):
        configured = [{"model": "custom-claude", "effort": "high"}]
        with patch("wechat_codex_multi.claude_models.discover_claude_model_options") as discover:
            options = claude_model_options({"claude": {"modelOptions": configured}})

        discover.assert_not_called()
        self.assertEqual(options, configured)


if __name__ == "__main__":
    unittest.main()
