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


if __name__ == "__main__":
    unittest.main()
