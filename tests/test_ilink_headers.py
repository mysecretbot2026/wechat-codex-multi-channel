import unittest
from unittest.mock import patch

from wechat_codex_multi.ilink import ILINK_APP_CLIENT_VERSION, ILINK_APP_ID
from wechat_codex_multi.login import login_with_qr
from wechat_codex_multi.wechat import WechatClient


class ILinkHeaderTests(unittest.TestCase):
    def test_wechat_client_sends_official_ilink_headers(self):
        headers = WechatClient("https://example.test", token="token")._headers("{}")

        self.assertEqual(headers["iLink-App-Id"], ILINK_APP_ID)
        self.assertEqual(headers["iLink-App-ClientVersion"], ILINK_APP_CLIENT_VERSION)

    def test_login_requests_send_official_ilink_headers(self):
        seen_headers = []

        def fake_fetch_json(url, headers=None, timeout_s=15):
            seen_headers.append(headers or {})
            if "get_bot_qrcode" in url:
                return {"qrcode": "qr-token", "qrcode_img_content": "qr-content"}
            return {
                "status": "confirmed",
                "bot_token": "token",
                "ilink_bot_id": "bot-id",
                "ilink_user_id": "user-id",
            }

        with patch("wechat_codex_multi.login.fetch_json", side_effect=fake_fetch_json), patch(
            "wechat_codex_multi.login.render_qr"
        ), patch("wechat_codex_multi.login.time.sleep"):
            login_with_qr("https://example.test", "3", "route", "/tmp")

        self.assertGreaterEqual(len(seen_headers), 2)
        for headers in seen_headers[:2]:
            self.assertEqual(headers["iLink-App-Id"], ILINK_APP_ID)
            self.assertEqual(headers["iLink-App-ClientVersion"], ILINK_APP_CLIENT_VERSION)
            self.assertEqual(headers["SKRouteTag"], "route")


if __name__ == "__main__":
    unittest.main()
