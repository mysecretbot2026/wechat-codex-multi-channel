import hashlib


def prompt_version(text):
    digest = hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"
