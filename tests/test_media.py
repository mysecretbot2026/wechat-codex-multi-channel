import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from wechat_codex_multi.media import MediaUploadError, cdn_upload, upload_file


class FakeWechatClient:
    def __init__(self, response):
        self.response = response

    def get_upload_url(self, **kwargs):
        return self.response


class MediaTests(unittest.TestCase):
    def test_upload_file_accepts_upload_full_url(self):
        with patch("wechat_codex_multi.media.cdn_upload", return_value="download-param") as cdn:
            upload = upload_file(
                FakeWechatClient({"upload_full_url": "https://example.test/upload"}),
                to_user_id="user",
                data=b"image-data",
                media_type=1,
            )

        self.assertEqual(upload["downloadEncryptedQueryParam"], "download-param")
        self.assertEqual(cdn.call_args.kwargs["upload_url"], "https://example.test/upload")

    def test_upload_file_error_includes_getuploadurl_response(self):
        with self.assertRaises(MediaUploadError) as ctx:
            upload_file(
                FakeWechatClient({"ret": 0, "err_msg": "missing upload url"}),
                to_user_id="user",
                data=b"image-data",
                media_type=1,
            )

        self.assertIn("upload_param or upload_full_url", str(ctx.exception))
        self.assertIn("missing upload url", str(ctx.exception))

    def test_cdn_upload_retries_transient_errors(self):
        class Headers:
            def get(self, name):
                return "download-param" if name == "x-encrypted-param" else None

        class Response:
            headers = Headers()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        with patch(
            "wechat_codex_multi.media.urllib.request.urlopen",
            side_effect=[TimeoutError("timeout"), Response()],
        ) as urlopen, patch("wechat_codex_multi.media.time.sleep") as sleep:
            result = cdn_upload("upload-param", "filekey", b"ciphertext")

        self.assertEqual(result, "download-param")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_cdn_upload_http_error_includes_context(self):
        error = HTTPError(
            url="https://example.test/upload",
            code=403,
            msg="Forbidden",
            hdrs={"x-error-message": "denied"},
            fp=None,
        )

        with patch("wechat_codex_multi.media.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(MediaUploadError) as ctx:
                cdn_upload("upload-param", "filekey", b"ciphertext")

        self.assertIn("HTTP 403", str(ctx.exception))
        self.assertIn("denied", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
