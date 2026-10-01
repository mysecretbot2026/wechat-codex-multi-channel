import tempfile
import json
import unittest
from pathlib import Path

from wechat_codex_multi.media_outbox import (
    acknowledge_media_outbox, queue_media, read_and_clear_media_outbox, read_media_outbox,
)


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

    def test_acknowledgement_preserves_newly_queued_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "report.pdf"
            file.write_bytes(b"pdf")
            outbox = Path(tmp) / "outbox.jsonl"
            queue_media(outbox, [str(file)])
            original = read_media_outbox(outbox)
            self.assertEqual(original, read_media_outbox(outbox))
            second = queue_media(outbox, [str(file)])
            acknowledge_media_outbox(outbox, [original[0]["id"]])
            remaining = read_media_outbox(outbox)
            self.assertEqual([item["id"] for item in remaining], [second[0]["id"]])

    def test_legacy_outbox_can_be_acknowledged_without_losing_pending_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            outbox.write_text(json.dumps({"kind": "file", "path": "/tmp/legacy.pdf"}) + "\n", encoding="utf-8")
            original = read_media_outbox(outbox)
            self.assertEqual(original, read_media_outbox(outbox))
            acknowledge_media_outbox(outbox, [original[0]["id"]])
            self.assertEqual(read_media_outbox(outbox), [])


if __name__ == "__main__":
    unittest.main()
