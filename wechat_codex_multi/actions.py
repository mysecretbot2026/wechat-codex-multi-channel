import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from .media import send_local_media


ACTION_RE = re.compile(r"\[\[(send_image|send_file|send_video):([^\]]+)\]\]")
BARE_ACTION_RE = re.compile(r"(?m)^\s*(send_image|send_file|send_video):(\S+)\s*$")
FILE_URL_RE = re.compile(r"file://[^\s]+")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
PLACEHOLDER_MEDIA_PATHS = {
    "/absolute/path/to/image.png",
    "/absolute/path/to/file.ext",
    "/absolute/path/to/video.mp4",
    "/path",
    "/path/from/generator.png",
    "真实绝对路径",
    "真实/图片/路径.png",
    "本地图片绝对路径",
    "本地文件绝对路径",
    "本地视频绝对路径",
    "/Users/bot/.../xxx.png",
}


def is_placeholder_media_path(path):
    normalized = (path or "").strip()
    return (
        not normalized
        or normalized in PLACEHOLDER_MEDIA_PATHS
        or normalized.startswith("/absolute/path/to/")
        or normalized.startswith("/path/from/generator")
        or "真实绝对路径" in normalized
        or "..." in normalized
        or "/xxx." in normalized
        or normalized.startswith("<")
    )


def normalize_media_path(path):
    normalized = (path or "").strip()
    if is_placeholder_media_path(normalized):
        return None
    parsed = urlparse(normalized)
    if parsed.scheme == "file":
        return unquote(parsed.path)
    return normalized


def kind_for_path(path):
    suffix = Path(path).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    return "file"


def extract_actions(text):
    actions = []
    for match in ACTION_RE.finditer(text or ""):
        action = match.group(1)
        path = normalize_media_path(match.group(2))
        if not path:
            continue
        kind = {"send_image": "image", "send_file": "file", "send_video": "video"}[action]
        actions.append({"kind": kind, "path": path})
    cleaned = ACTION_RE.sub("", text or "")
    for match in BARE_ACTION_RE.finditer(cleaned):
        action = match.group(1)
        path = normalize_media_path(match.group(2))
        if not path:
            continue
        kind = {"send_image": "image", "send_file": "file", "send_video": "video"}[action]
        actions.append({"kind": kind, "path": path})
    cleaned = BARE_ACTION_RE.sub("", cleaned)
    for match in FILE_URL_RE.finditer(cleaned):
        path = normalize_media_path(match.group(0))
        if path:
            actions.append({"kind": kind_for_path(path), "path": path})
    cleaned = FILE_URL_RE.sub("", cleaned).strip()
    return cleaned, actions


def validate_media_action(action, max_file_bytes):
    normalized = normalize_media_path(action["path"])
    if not normalized:
        return None
    path = Path(normalized).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise RuntimeError(f"媒体文件不存在: {path}")
    size = path.stat().st_size
    if size > max_file_bytes:
        raise RuntimeError(f"媒体文件过大: {path} ({size} bytes)")
    return str(path)


def execute_actions(wechat_client, user_id, context_token, actions, max_file_bytes, transfer_semaphore=None):
    sent = []
    for action in actions:
        if is_placeholder_media_path(action.get("path")):
            continue
        path = validate_media_action(action, max_file_bytes)
        if not path:
            continue
        if transfer_semaphore is None:
            send_local_media(wechat_client, user_id, context_token, path, action["kind"])
        else:
            with transfer_semaphore:
                send_local_media(wechat_client, user_id, context_token, path, action["kind"])
        sent.append(path)
    return sent
