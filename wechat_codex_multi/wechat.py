import hashlib
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .util import generate_id, random_wechat_uin


CHANNEL_VERSION = "wechat-codex-multi-channel/0.1.0"
LONG_POLL_TIMEOUT_MS = 35_000

MESSAGE_TYPE_USER = 1
MESSAGE_TYPE_BOT = 2
MESSAGE_STATE_FINISH = 2
TYPING_STATUS_TYPING = 1
TYPING_STATUS_CANCEL = 2
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5
UPLOAD_IMAGE = 1
UPLOAD_VIDEO = 2
UPLOAD_FILE = 3


class WechatApiError(RuntimeError):
    pass


class WechatClient:
    def __init__(self, base_url, token=None, route_tag=None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.route_tag = route_tag

    def _headers(self, body):
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Content-Length": str(len(body.encode("utf-8"))),
            "X-WECHAT-UIN": random_wechat_uin(),
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.route_tag:
            headers["SKRouteTag"] = self.route_tag
        return headers

    def post_json(self, endpoint, payload, timeout_s=15):
        body = json.dumps(payload, ensure_ascii=False)
        request = urllib.request.Request(
            url=f"{self.base_url}/{endpoint.lstrip('/')}",
            method="POST",
            data=body.encode("utf-8"),
            headers=self._headers(body),
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8", errors="replace")
            raise WechatApiError(f"HTTP {err.code}: {raw[:300]}") from err
        data = json.loads(raw or "{}")
        ret = data.get("ret")
        errcode = data.get("errcode")
        if ret not in (None, 0) or errcode not in (None, 0):
            raise WechatApiError(json.dumps(data, ensure_ascii=False)[:500])
        return data

    def get_updates(self, get_updates_buf=""):
        try:
            return self.post_json(
                "ilink/bot/getupdates",
                {
                    "get_updates_buf": get_updates_buf or "",
                    "base_info": {"channel_version": CHANNEL_VERSION},
                },
                timeout_s=(LONG_POLL_TIMEOUT_MS + 5_000) / 1000,
            )
        except (TimeoutError, socket.timeout, urllib.error.URLError) as err:
            reason = getattr(err, "reason", err)
            if isinstance(reason, socket.timeout) or isinstance(err, TimeoutError):
                return {"ret": 0, "msgs": [], "get_updates_buf": get_updates_buf}
            raise

    def send_message_item(self, to_user_id, context_token, item):
        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": generate_id("wechat-codex"),
                "message_type": MESSAGE_TYPE_BOT,
                "message_state": MESSAGE_STATE_FINISH,
                "item_list": [item],
                "context_token": context_token,
            },
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        return self.post_json("ilink/bot/sendmessage", payload, timeout_s=20)

    def send_text(self, to_user_id, context_token, text):
        return self.send_message_item(
            to_user_id,
            context_token,
            {
                "type": ITEM_TEXT,
                "text_item": {"text": text},
            },
        )

    def get_config(self, to_user_id, context_token):
        return self.post_json(
            "ilink/bot/getconfig",
            {
                "ilink_user_id": to_user_id,
                "context_token": context_token,
                "base_info": {"channel_version": CHANNEL_VERSION},
            },
            timeout_s=10,
        )

    def send_typing(self, to_user_id, typing_ticket, status=TYPING_STATUS_TYPING):
        return self.post_json(
            "ilink/bot/sendtyping",
            {
                "ilink_user_id": to_user_id,
                "typing_ticket": typing_ticket,
                "status": status,
                "base_info": {"channel_version": CHANNEL_VERSION},
            },
            timeout_s=10,
        )

    def get_upload_url(self, to_user_id, filekey, media_type, rawsize, rawfilemd5, filesize, aeskey_hex):
        return self.post_json(
            "ilink/bot/getuploadurl",
            {
                "filekey": filekey,
                "media_type": media_type,
                "to_user_id": to_user_id,
                "rawsize": rawsize,
                "rawfilemd5": rawfilemd5,
                "filesize": filesize,
                "no_need_thumb": True,
                "aeskey": aeskey_hex,
                "base_info": {"channel_version": CHANNEL_VERSION},
            },
            timeout_s=20,
        )


def extract_text(msg, media_dir=None, download_media=True):
    parts = []
    for item in msg.get("item_list") or []:
        item_type = item.get("type")
        if item_type == ITEM_TEXT:
            text = (item.get("text_item") or {}).get("text")
            if text:
                parts.append(text)
        elif item_type == ITEM_VOICE:
            text = (item.get("voice_item") or {}).get("text")
            if text:
                parts.append(text)
        elif item_type == ITEM_IMAGE:
            if download_media:
                path = _download_inbound_item(item, media_dir)
                parts.append(f"[收到图片：{path}]" if path else "[收到图片：下载失败或缺少媒体信息]")
            else:
                parts.append("[收到图片]")
        elif item_type == ITEM_FILE:
            file_name = (item.get("file_item") or {}).get("file_name") or "未命名文件"
            if download_media:
                path = _download_inbound_item(item, media_dir)
                parts.append(f"[收到文件：{file_name}；本地路径：{path}]" if path else f"[收到文件：{file_name}；下载失败或缺少媒体信息]")
            else:
                parts.append(f"[收到文件：{file_name}]")
        elif item_type == ITEM_VIDEO:
            if download_media:
                path = _download_inbound_item(item, media_dir)
                parts.append(f"[收到视频：{path}]" if path else "[收到视频：下载失败或缺少媒体信息]")
            else:
                parts.append("[收到视频]")
    return "\n".join(parts).strip()


def _download_inbound_item(item, media_dir):
    if not media_dir:
        return None
    from .media import download_inbound_media

    try:
        saved = download_inbound_media(item, Path(media_dir))
    except Exception:
        return None
    return saved.get("path") if saved else None


def md5_hex(data):
    return hashlib.md5(data).hexdigest()
