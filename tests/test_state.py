import json
import tempfile
import unittest

from wechat_codex_multi.state import StateStore


class StateStoreTests(unittest.TestCase):
    def test_update_account_skips_unchanged_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(tmp, save_debounce_ms=0)
            state.upsert_account({"accountId": "acct-1", "getUpdatesBuf": "buf-1"})
            before = state.file.stat().st_mtime_ns

            changed = state.update_account("acct-1", getUpdatesBuf="buf-1")

            self.assertFalse(changed)
            self.assertEqual(state.file.stat().st_mtime_ns, before)

    def test_upsert_account_keeps_distinct_accounts_with_same_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(tmp, save_debounce_ms=0)
            state.upsert_account({"accountId": "acct-1", "userId": "user-1", "token": "token-1"})
            state.upsert_account({"accountId": "acct-2", "userId": "user-1", "token": "token-2"})

            reloaded = StateStore(tmp, save_debounce_ms=0)
            accounts = sorted(reloaded.list_accounts(), key=lambda item: item["accountId"])

            self.assertEqual([item["accountId"] for item in accounts], ["acct-1", "acct-2"])
            self.assertEqual([item["token"] for item in accounts], ["token-1", "token-2"])
            self.assertEqual([item["nickname"] for item in accounts], ["用户1", "用户2"])

    def test_upsert_account_replaces_matching_account_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(tmp, save_debounce_ms=0)
            state.upsert_account({"accountId": "acct-1", "userId": "user-1", "token": "old"})
            state.upsert_account({"accountId": "acct-1", "userId": "user-2", "token": "new"})

            accounts = state.list_accounts()

            self.assertEqual(len(accounts), 1)
            self.assertEqual(accounts[0]["userId"], "user-2")
            self.assertEqual(accounts[0]["token"], "new")
            self.assertEqual(accounts[0]["nickname"], "用户1")

    def test_account_nickname_can_be_set_renamed_and_used_for_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(tmp, save_debounce_ms=0)
            state.upsert_account({"accountId": "acct-1", "userId": "user-1", "token": "token-1", "nickname": "小号"})
            state.upsert_account({"accountId": "acct-2", "userId": "user-2", "token": "token-2"})
            state.update_session("acct-1:user-1", cwd="/tmp")
            state.set_context_token("acct-1", "user-1", "ctx")
            state.upsert_workspace("acct-1:user-1", "a", "/tmp/a")

            renamed = state.rename_account("小号", "主号")
            deleted = state.delete_account("主号")

            self.assertEqual(renamed["accountId"], "acct-1")
            self.assertEqual(deleted["accountId"], "acct-1")
            self.assertEqual([item["accountId"] for item in state.list_accounts()], ["acct-2"])
            self.assertNotIn("acct-1:user-1", state.state["sessions"])
            self.assertNotIn("acct-1:user-1", state.state["contextTokens"])
            self.assertNotIn("acct-1:user-1", state.state["workspaces"])

    def test_account_nickname_must_be_unique(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(tmp, save_debounce_ms=0)
            state.upsert_account({"accountId": "acct-1", "nickname": "主号"})

            with self.assertRaisesRegex(ValueError, "用户昵称已存在"):
                state.upsert_account({"accountId": "acct-2", "nickname": "主号"})

    def test_debounced_context_token_flushes_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(tmp, save_debounce_ms=60_000)

            changed = state.set_context_token("acct-1", "user-1", "token-1")
            self.assertTrue(changed)
            self.assertFalse(state.file.exists())

            state.flush()

            data = json.loads(state.file.read_text(encoding="utf-8"))
            self.assertEqual(data["contextTokens"]["acct-1:user-1"], "token-1")

    def test_workspace_state_is_scoped_to_base_conversation(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(tmp, save_debounce_ms=0)
            base = state.conversation_key("acct-1", "user-1")

            state.upsert_workspace(base, "a", "/tmp/project-a")
            state.upsert_workspace(base, "b", "/tmp/project-b")
            state.set_active_workspace(base, "b")

            self.assertEqual(state.get_active_workspace(base), "b")
            self.assertEqual(state.workspace_conversation_key(base, "a"), "acct-1:user-1:a")
            self.assertEqual([item["name"] for item in state.list_workspaces(base)], ["a", "b"])
            self.assertEqual(state.get_workspace(base, "a")["cwd"], "/tmp/project-a")


if __name__ == "__main__":
    unittest.main()
