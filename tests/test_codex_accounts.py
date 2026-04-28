import unittest

from wechat_codex_multi.codex_accounts import (
    adjacent_codex_account,
    find_codex_account,
    normalize_codex_accounts,
    resolve_session_codex_account,
)


class CodexAccountsTests(unittest.TestCase):
    def test_normalize_codex_accounts_adds_default(self):
        config = {"codex": {}}

        normalize_codex_accounts(config)

        self.assertEqual(config["codex"]["defaultAccount"], "main")
        self.assertEqual(config["codex"]["accounts"][0]["name"], "main")
        self.assertTrue(config["codex"]["accounts"][0]["codexHome"].endswith(".codex"))

    def test_resolve_session_account_falls_back_to_default(self):
        config = {
            "codex": {
                "defaultAccount": "main",
                "accounts": [
                    {"name": "main", "codexHome": "~/.codex"},
                    {"name": "alt", "codexHome": "/tmp/codex-alt"},
                ],
            }
        }
        normalize_codex_accounts(config)

        account = resolve_session_codex_account(config, {"codexAccount": "missing"})

        self.assertEqual(account["name"], "main")

    def test_find_codex_account_by_index_name_and_prefix(self):
        config = {
            "codex": {
                "accounts": [
                    {"name": "main", "codexHome": "~/.codex"},
                    {"name": "backup", "codexHome": "/tmp/codex-backup"},
                    {"name": "work", "codexHome": "/tmp/codex-work"},
                ]
            }
        }
        normalize_codex_accounts(config)

        self.assertEqual(find_codex_account(config, "2")["name"], "backup")
        self.assertEqual(find_codex_account(config, "work")["name"], "work")
        self.assertEqual(find_codex_account(config, "bac")["name"], "backup")
        self.assertIsNone(find_codex_account(config, "9"))

    def test_adjacent_codex_account_wraps(self):
        config = {
            "codex": {
                "accounts": [
                    {"name": "main", "codexHome": "~/.codex"},
                    {"name": "backup", "codexHome": "/tmp/codex-backup"},
                ]
            }
        }
        normalize_codex_accounts(config)

        self.assertEqual(adjacent_codex_account(config, "main", 1)["name"], "backup")
        self.assertEqual(adjacent_codex_account(config, "main", -1)["name"], "backup")


if __name__ == "__main__":
    unittest.main()
