import unittest

from wechat_codex_multi.claude_accounts import (
    find_claude_account,
    normalize_claude_accounts,
    resolve_session_claude_account,
)


class ClaudeAccountsTests(unittest.TestCase):
    def test_default_account_does_not_force_config_dir(self):
        config = {"claude": {"accounts": [{"name": "main", "claudeConfigDir": ""}]}}

        normalize_claude_accounts(config)

        self.assertEqual(config["claude"]["accounts"][0]["claudeConfigDir"], "")

    def test_find_account_by_index_name_or_prefix(self):
        config = {
            "claude": {
                "accounts": [
                    {"name": "main", "claudeConfigDir": ""},
                    {"name": "work", "claudeConfigDir": "/tmp/claude-work"},
                ]
            }
        }
        normalize_claude_accounts(config)

        self.assertEqual(find_claude_account(config, "2")["name"], "work")
        self.assertEqual(find_claude_account(config, "work")["name"], "work")
        self.assertEqual(find_claude_account(config, "wo")["name"], "work")

    def test_resolve_session_account_falls_back_to_default(self):
        config = {"claude": {"defaultAccount": "main", "accounts": [{"name": "main", "claudeConfigDir": ""}]}}
        normalize_claude_accounts(config)

        account = resolve_session_claude_account(config, {"claudeAccount": "missing"})

        self.assertEqual(account["name"], "main")


if __name__ == "__main__":
    unittest.main()
