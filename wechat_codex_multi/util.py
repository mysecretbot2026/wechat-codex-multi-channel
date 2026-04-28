import base64
import os
import re
import secrets
import textwrap
import time


def random_wechat_uin():
    value = int.from_bytes(os.urandom(4), "big")
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def generate_id(prefix):
    return f"{prefix}:{int(time.time() * 1000)}-{secrets.token_hex(4)}"


def split_text(text, limit=4000):
    text = str(text or "")
    if len(text) <= limit:
        return [text] if text else []
    chunks = []
    current = []
    size = 0
    for part in re.split(r"(?<=[。！？.!?\n])", text):
        if not part:
            continue
        if size + len(part) > limit and current:
            chunks.append("".join(current).strip())
            current = []
            size = 0
        if len(part) > limit:
            chunks.extend(textwrap.wrap(part, width=limit, break_long_words=False) or [part])
            continue
        current.append(part)
        size += len(part)
    if current:
        chunks.append("".join(current).strip())
    return [c for c in chunks if c]


def markdown_to_plain_text(text):
    result = str(text or "")
    result = re.sub(r"```[^\n]*\n?([\s\S]*?)```", lambda m: m.group(1).strip(), result)
    result = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", result)
    result = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", result)
    result = re.sub(r"^\|[\s:|-]+\|$", "", result, flags=re.MULTILINE)
    result = re.sub(
        r"^\|(.+)\|$",
        lambda m: "  ".join(cell.strip() for cell in m.group(1).split("|")),
        result,
        flags=re.MULTILINE,
    )
    result = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", result)
    result = re.sub(r"\*\*([^*]+)\*\*", r"\1", result)
    result = re.sub(r"\*([^*]+)\*", r"\1", result)
    result = re.sub(r"__([^_]+)__", r"\1", result)
    result = re.sub(r"_([^_]+)_", r"\1", result)
    result = re.sub(r"`([^`]+)`", r"\1", result)
    result = re.sub(r"^#{1,6}\s+", "", result, flags=re.MULTILINE)
    result = re.sub(r"^[-*_]{3,}$", "", result, flags=re.MULTILINE)
    result = re.sub(r"^>\s?", "", result, flags=re.MULTILINE)
    result = re.sub(r"^[\s]*[-*+]\s+", "- ", result, flags=re.MULTILINE)
    return result.strip()


def redact(value, keep=4):
    text = str(value or "")
    if len(text) <= keep * 2:
        return "***"
    return f"{text[:keep]}...{text[-keep:]}"
