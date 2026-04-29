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
