import base64
import json
import os
import secrets
import subprocess
import time
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path

from .wechat import ITEM_FILE, ITEM_IMAGE, ITEM_VIDEO, ITEM_VOICE, UPLOAD_FILE, UPLOAD_IMAGE, UPLOAD_VIDEO, md5_hex


CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
MAX_CDN_UPLOAD_RETRIES = 3


class MediaUploadError(RuntimeError):
    pass


def aes_ecb_padded_size(plaintext_size):
    return ((plaintext_size + 1 + 15) // 16) * 16


def aes_ecb_encrypt(data, key):
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad

        return AES.new(key, AES.MODE_ECB).encrypt(pad(data, 16))
    except Exception:
        pass

    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7

        padder = PKCS7(128).padder()
        padded = padder.update(data) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        return encryptor.update(padded) + encryptor.finalize()
    except Exception:
        pass

    completed = subprocess.run(
        ["openssl", "enc", "-aes-128-ecb", "-K", key.hex(), "-nosalt"],
        input=data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise MediaUploadError(completed.stderr.decode("utf-8", errors="replace") or "openssl AES failed")
    return completed.stdout


def parse_aes_key(aes_key_base64):
    decoded = base64.b64decode(aes_key_base64)
    if len(decoded) == 16:
        return decoded
    return bytes.fromhex(decoded.decode("ascii"))


def aes_ecb_decrypt(ciphertext, key):
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad

        return unpad(AES.new(key, AES.MODE_ECB).decrypt(ciphertext), 16)
    except Exception:
        pass

    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7

        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except Exception:
        pass

    completed = subprocess.run(
        ["openssl", "enc", "-d", "-aes-128-ecb", "-K", key.hex(), "-nosalt"],
        input=ciphertext,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise MediaUploadError(completed.stderr.decode("utf-8", errors="replace") or "openssl AES decrypt failed")
    return completed.stdout


def cdn_download_decrypt(encrypted_query_param, aes_key_base64):
    url = f"{CDN_BASE_URL}/download?encrypted_query_param={urllib.parse.quote(encrypted_query_param, safe='')}"
    request = urllib.request.Request(url=url, method="GET")
    with urllib.request.urlopen(request, timeout=60) as response:
        ciphertext = response.read()
    return aes_ecb_decrypt(ciphertext, parse_aes_key(aes_key_base64))


def cdn_upload(upload_param, filekey, ciphertext, upload_url=""):
    url = upload_url.strip() if upload_url else (
        f"{CDN_BASE_URL}/upload?"
        f"encrypted_query_param={urllib.parse.quote(upload_param, safe='')}"
        f"&filekey={urllib.parse.quote(filekey, safe='')}"
    )
    last_error = None
    for attempt in range(1, MAX_CDN_UPLOAD_RETRIES + 1):
        request = urllib.request.Request(
            url=url,
            method="POST",
            data=ciphertext,
            headers={"Content-Type": "application/octet-stream"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                download_param = response.headers.get("x-encrypted-param")
                if not download_param:
                    raise MediaUploadError("CDN upload missing x-encrypted-param")
                return download_param
        except urllib.error.HTTPError as err:
            try:
                body = err.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                body = ""
            message = err.headers.get("x-error-message") or body or f"HTTP {err.code}"
            raise MediaUploadError(f"CDN upload HTTP {err.code}: {message}") from err
        except Exception as err:
            last_error = err
            if attempt >= MAX_CDN_UPLOAD_RETRIES:
                break
            time.sleep(attempt)
    raise MediaUploadError(f"CDN upload failed after retries: {last_error}") from last_error


def upload_file(wechat_client, to_user_id, data, media_type):
    filekey = secrets.token_hex(16)
    aes_key = os.urandom(16)
    aeskey_hex = aes_key.hex()
    upload_resp = wechat_client.get_upload_url(
        to_user_id=to_user_id,
        filekey=filekey,
        media_type=media_type,
        rawsize=len(data),
        rawfilemd5=md5_hex(data),
        filesize=aes_ecb_padded_size(len(data)),
        aeskey_hex=aeskey_hex,
    )
    upload_param = upload_resp.get("upload_param") or ""
    upload_url = upload_resp.get("upload_full_url") or ""
    if not (upload_param or upload_url):
        detail = json.dumps(upload_resp, ensure_ascii=False)[:500]
        raise MediaUploadError(f"getuploadurl did not return upload_param or upload_full_url: {detail}")
    ciphertext = aes_ecb_encrypt(data, aes_key)
    download_param = cdn_upload(upload_param, filekey, ciphertext, upload_url=upload_url)
    return {
        "downloadEncryptedQueryParam": download_param,
        "aeskeyHex": aeskey_hex,
        "aeskeyBase64": base64.b64encode(aeskey_hex.encode("ascii")).decode("ascii"),
        "fileSize": len(data),
        "fileSizeCiphertext": len(ciphertext),
    }


def build_image_item(upload):
    return {
        "type": ITEM_IMAGE,
        "image_item": {
            "media": {
                "encrypt_query_param": upload["downloadEncryptedQueryParam"],
                "aes_key": upload["aeskeyBase64"],
                "encrypt_type": 1,
            },
            "mid_size": upload["fileSizeCiphertext"],
        },
    }


def build_file_item(upload, file_name):
    return {
        "type": ITEM_FILE,
        "file_item": {
            "media": {
                "encrypt_query_param": upload["downloadEncryptedQueryParam"],
                "aes_key": upload["aeskeyBase64"],
                "encrypt_type": 1,
            },
            "file_name": file_name,
            "len": str(upload["fileSize"]),
        },
    }


def build_video_item(upload):
    return {
        "type": ITEM_VIDEO,
        "video_item": {
            "media": {
                "encrypt_query_param": upload["downloadEncryptedQueryParam"],
                "aes_key": upload["aeskeyBase64"],
                "encrypt_type": 1,
            },
            "video_size": upload["fileSizeCiphertext"],
        },
    }


def send_local_media(wechat_client, to_user_id, context_token, path, kind):
    file_path = Path(path).expanduser().resolve()
    data = file_path.read_bytes()
    if kind == "image":
        upload = upload_file(wechat_client, to_user_id, data, UPLOAD_IMAGE)
        item = build_image_item(upload)
    elif kind == "video":
        upload = upload_file(wechat_client, to_user_id, data, UPLOAD_VIDEO)
        item = build_video_item(upload)
    else:
        upload = upload_file(wechat_client, to_user_id, data, UPLOAD_FILE)
        item = build_file_item(upload, file_path.name)
    return wechat_client.send_message_item(to_user_id, context_token, item)


def inbound_media_extension(kind, item):
    if kind == "image":
        return ".jpg"
    if kind == "video":
        return ".mp4"
    if kind == "voice":
        return ".silk"
    name = (item.get("file_item") or {}).get("file_name") or ""
    return Path(name).suffix or ".bin"


def download_inbound_media(item, media_dir):
    item_type = item.get("type")
    if item_type == ITEM_IMAGE:
        kind = "image"
        payload = item.get("image_item") or {}
    elif item_type == ITEM_FILE:
        kind = "file"
        payload = item.get("file_item") or {}
    elif item_type == ITEM_VIDEO:
        kind = "video"
        payload = item.get("video_item") or {}
    elif item_type == ITEM_VOICE:
        kind = "voice"
        payload = item.get("voice_item") or {}
    else:
        return None

    media = payload.get("media") or {}
    encrypted_query_param = media.get("encrypt_query_param")
    aes_key = media.get("aes_key") or payload.get("aeskey")
    if not encrypted_query_param or not aes_key:
        return None

    data = cdn_download_decrypt(encrypted_query_param, aes_key)
    media_dir = Path(media_dir).expanduser().resolve()
    media_dir.mkdir(parents=True, exist_ok=True)
    path = media_dir / f"{int(time.time() * 1000)}-{secrets.token_hex(4)}{inbound_media_extension(kind, item)}"
    path.write_bytes(data)
    return {"kind": kind, "path": str(path)}
