import tempfile
import unittest
from pathlib import Path

from wechat_codex_multi.actions import extract_actions, execute_actions


class ActionTests(unittest.TestCase):
    def test_extract_actions_ignores_placeholder_media_paths(self):
        text = "\n".join(
            [
                "示例：",
                "[[send_image:/absolute/path/to/image.png]]",
                "[[send_file:/absolute/path/to/file.ext]]",
                "[[send_video:/absolute/path/to/video.mp4]]",
            ]
        )

        cleaned, actions = extract_actions(text)

        self.assertEqual(actions, [])
        self.assertEqual(cleaned, "示例：")

    def test_extract_actions_keeps_real_media_path(self):
        cleaned, actions = extract_actions("完成\n[[send_image:/tmp/real-image.png]]")

        self.assertEqual(cleaned, "完成")
        self.assertEqual(actions, [{"kind": "image", "path": "/tmp/real-image.png"}])

    def test_extract_actions_accepts_bare_media_marker_line(self):
        cleaned, actions = extract_actions("完成\nsend_image:/tmp/real-image.png")

        self.assertEqual(cleaned, "完成")
        self.assertEqual(actions, [{"kind": "image", "path": "/tmp/real-image.png"}])

    def test_extract_actions_ignores_ellipsis_example_paths(self):
        text = "\n".join(
            [
                "例如：",
                "[[send_image:/Users/bot/.../xxx.png]]",
                "Saved to: file:///Users/bot/.../xxx.png",
            ]
        )

        cleaned, actions = extract_actions(text)

        self.assertEqual(actions, [])
        self.assertEqual(cleaned, "例如：\n\nSaved to:")

    def test_execute_actions_skips_placeholder_media_paths(self):
        sent = execute_actions(
            wechat_client=object(),
            user_id="user",
            context_token="token",
            actions=[{"kind": "image", "path": "/absolute/path/to/image.png"}],
            max_file_bytes=1024,
        )

        self.assertEqual(sent, [])

    def test_execute_actions_skips_chinese_placeholder_media_paths(self):
        sent = execute_actions(
            wechat_client=object(),
            user_id="user",
            context_token="token",
            actions=[{"kind": "image", "path": "真实绝对路径"}],
            max_file_bytes=1024,
        )

        self.assertEqual(sent, [])

    def test_extract_actions_normalizes_file_url_marker(self):
        cleaned, actions = extract_actions("完成\n[[send_image:file:///tmp/real-image.png]]")

        self.assertEqual(cleaned, "完成")
        self.assertEqual(actions, [{"kind": "image", "path": "/tmp/real-image.png"}])

    def test_extract_actions_finds_codex_saved_file_url(self):
        cleaned, actions = extract_actions("Saved to: file:///tmp/generated.png")

        self.assertEqual(cleaned, "Saved to:")
        self.assertEqual(actions, [{"kind": "image", "path": "/tmp/generated.png"}])

    def test_execute_actions_still_validates_real_paths(self):
        with tempfile.NamedTemporaryFile() as file:
            path = Path(file.name)
            with self.assertRaises(RuntimeError):
                execute_actions(
                    wechat_client=object(),
                    user_id="user",
                    context_token="token",
                    actions=[{"kind": "image", "path": str(path) + "-missing"}],
                    max_file_bytes=1024,
                )


if __name__ == "__main__":
    unittest.main()
