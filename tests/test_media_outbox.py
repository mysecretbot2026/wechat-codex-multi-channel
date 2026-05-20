import tempfile
import unittest
from pathlib import Path

from wechat_codex_multi.media_outbox import queue_media, read_and_clear_media_outbox


class MediaOutboxTests(unittest.TestCase):
    def test_queue_media_writes_actions_and_clears_after_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "image.png"
            media.write_bytes(b"png")
            outbox = Path(tmp) / "outbox.jsonl"

            queued = queue_media(outbox, [str(media)])
            actions = read_and_clear_media_outbox(outbox)
            second = read_and_clear_media_outbox(outbox)

            self.assertEqual(queued[0]["kind"], "image")
            self.assertEqual(actions, [{"kind": "image", "path": str(media.resolve())}])
            self.assertEqual(second, [])

    def test_queue_media_rejects_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                queue_media(Path(tmp) / "outbox.jsonl", [str(Path(tmp) / "missing.pdf")])


if __name__ == "__main__":
    unittest.main()
