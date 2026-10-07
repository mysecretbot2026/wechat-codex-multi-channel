import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_codex_multi.codex_runtime import _desktop_candidates, desktop_codex_bin, resolve_codex_bin
from wechat_codex_multi.config import load_config


class CodexBinaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for mock in (
            patch.dict(os.environ, {"PATH": str(self.root)}),
            patch("wechat_codex_multi.codex_runtime._desktop_candidates", return_value=[]),
            patch("wechat_codex_multi.codex_runtime._executable", side_effect=lambda path:
                  str(path).startswith(str(self.root)) and path.is_file() and os.access(path, os.X_OK)),
        ):
            mock.start()
            self.addCleanup(mock.stop)

    def binary(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!{sys.executable}\n", encoding="utf-8")
        path.chmod(0o700)
        return str(path)

    def test_default_and_missing_legacy_chatgpt_path_find_installed_cli(self):
        cli = self.binary("codex")
        self.assertEqual(resolve_codex_bin(), cli)
        self.assertEqual(resolve_codex_bin("/Applications/ChatGPT.app/Contents/Resources/codex"), cli)

    def test_desktop_update_relocates_legacy_path_to_nested_bundle(self):
        cli = self.binary("codex")
        bundled = self.binary("ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")
        with patch("wechat_codex_multi.codex_runtime._desktop_candidates", return_value=[Path(bundled)]):
            legacy = "/Applications/ChatGPT.app/Contents/Resources/codex"
            self.assertEqual(resolve_codex_bin(legacy, prefer_desktop=True), bundled)
            self.assertEqual(resolve_codex_bin(legacy), cli)
            self.assertEqual(desktop_codex_bin({"codex": {"bin": cli}}), bundled)

    def test_only_desktop_installation_is_usable_without_cli(self):
        bundled = self.binary("Codex.app/Contents/Resources/codex")
        with patch("wechat_codex_multi.codex_runtime._desktop_candidates", return_value=[Path(bundled)]):
            self.assertEqual(resolve_codex_bin(), bundled)

    def test_candidate_inventory_includes_both_apps_and_user_applications(self):
        candidates = _desktop_candidates()
        self.assertIn(Path("/Applications/Codex.app/Contents/Resources/codex"), candidates)
        self.assertIn(Path.home() / "Applications/ChatGPT.app/Contents/Resources/codex", candidates)

    def test_explicit_binary_expands_environment(self):
        binary = self.binary("tools/custom-codex")
        with patch.dict(os.environ, {"CODEX_TEST_PREFIX": str(self.root)}):
            self.assertEqual(resolve_codex_bin("$CODEX_TEST_PREFIX/tools/custom-codex"), binary)

    def test_missing_custom_binary_does_not_silently_select_installed_cli(self):
        self.binary("codex")
        with self.assertRaisesRegex(FileNotFoundError, "codex.bin"):
            resolve_codex_bin(str(self.root / "custom-codex"))

    def test_non_executable_bundle_and_missing_install_give_actionable_error(self):
        path = self.root / "ChatGPT.app/Contents/Resources/codex"
        path.parent.mkdir(parents=True)
        path.write_text("not executable")
        with patch("wechat_codex_multi.codex_runtime._desktop_candidates", return_value=[path]):
            with self.assertRaisesRegex(FileNotFoundError, "npm install -g @openai/codex"):
                resolve_codex_bin()


class CodexConfigHomeTests(unittest.TestCase):
    def test_implicit_main_home_matches_cli_environment(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CODEX_HOME": tmp}):
            config = load_config(Path(tmp) / "missing.json")
            self.assertEqual(config["codex"]["accounts"], [{"name": "main", "codexHome": str(Path(tmp).resolve())}])

    def test_explicit_account_and_legacy_home_override_cli_environment(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CODEX_HOME": tmp + "/environment"}):
            file = Path(tmp) / "config.json"
            for codex in (
                {"accounts": [{"name": "main", "codexHome": tmp + "/configured"}]},
                {"codexHome": tmp + "/configured"},
            ):
                with self.subTest(codex=codex):
                    file.write_text(json.dumps({"codex": codex}), encoding="utf-8")
                    self.assertEqual(load_config(file)["codex"]["accounts"][0]["codexHome"], str(Path(tmp, "configured").resolve()))

    def test_example_starts_with_only_main_account(self):
        file = Path(__file__).resolve().parent.parent / "config.example.json"
        self.assertEqual([item["name"] for item in load_config(file)["codex"]["accounts"]], ["main"])
