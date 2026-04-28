import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


LOGIN_TIMEOUT_MS = 480_000
STATUS_TIMEOUT_MS = 35_000


def fetch_json(url, headers=None, timeout_s=15):
    req = urllib.request.Request(url=url, method="GET", headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout_s) as response:
        return json.loads(response.read().decode("utf-8"))


def render_qr(qr_content, project_dir):
    script = "require('qrcode-terminal').generate(process.argv[1], { small: true });"
    try:
        completed = subprocess.run(
            ["node", "-e", script, qr_content],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        if completed.stdout.strip():
            print(completed.stdout.strip("\n"))
            return
    except Exception:
        pass
    print(qr_content)


def login_with_qr(base_url, bot_type, route_tag, project_dir):
    base = base_url.rstrip("/")
    headers = {"SKRouteTag": route_tag} if route_tag else {}
    qr = fetch_json(f"{base}/ilink/bot/get_bot_qrcode?bot_type={urllib.parse.quote(bot_type)}", headers=headers)
    qr_content = qr.get("qrcode_img_content")
    if not qr_content:
        raise RuntimeError("二维码内容缺失")
    render_qr(qr_content, project_dir)
    print("\n请使用微信扫描二维码并确认登录。")
    deadline = time.time() + LOGIN_TIMEOUT_MS / 1000
    scanned = False
    while time.time() < deadline:
        encoded = urllib.parse.quote(qr["qrcode"], safe="")
        try:
            status = fetch_json(
                f"{base}/ilink/bot/get_qrcode_status?qrcode={encoded}",
                headers={**headers, "iLink-App-ClientVersion": "1"},
                timeout_s=STATUS_TIMEOUT_MS / 1000,
            )
        except (TimeoutError, socket.timeout, urllib.error.URLError) as err:
            reason = getattr(err, "reason", err)
            if isinstance(reason, socket.timeout) or isinstance(err, socket.timeout):
                status = {"status": "wait"}
            else:
                raise
        current = status.get("status")
        if current == "wait":
            sys.stdout.write(".")
            sys.stdout.flush()
        elif current == "scaned" and not scanned:
            print("\n已扫码，请在微信中确认...")
            scanned = True
        elif current == "expired":
            raise RuntimeError("二维码已过期，请重新运行 add-account")
        elif current == "confirmed":
            if not status.get("ilink_bot_id") or not status.get("bot_token"):
                raise RuntimeError("登录失败：服务端未返回完整凭据")
            return {
                "token": status["bot_token"],
                "accountId": status["ilink_bot_id"],
                "baseUrl": status.get("baseurl") or base_url,
                "userId": status.get("ilink_user_id"),
                "getUpdatesBuf": "",
                "savedAt": int(time.time() * 1000),
            }
        time.sleep(1)
    raise RuntimeError("登录超时")
