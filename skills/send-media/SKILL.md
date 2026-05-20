---
name: send-media
description: Use when an Agent needs to send a local image, video, document, PDF, archive, or other file to the WeChat user from this project. Register media with the project CLI instead of writing send_image/send_file/send_video marker text in the final reply.
---

# Send Media

Use this skill whenever a local file should be sent to the WeChat user.

Preferred command for one file:

```bash
python3 -m wechat_codex_multi media-send /absolute/path/to/file
```

Multiple files are allowed:

```bash
python3 -m wechat_codex_multi media-send /absolute/path/a.png /absolute/path/report.pdf
python3 -m wechat_codex_multi media-send /absolute/path/a.png /absolute/path/b.mp4 /absolute/path/archive.zip
```

When sending mixed file types in one command, omit `--kind`. The project will infer:

- image for common image extensions
- video for common video extensions
- file for documents, PDFs, archives, and unknown extensions

Optional explicit kind:

```bash
python3 -m wechat_codex_multi media-send --kind image /absolute/path/to/image.png
python3 -m wechat_codex_multi media-send --kind video /absolute/path/to/video.mp4
python3 -m wechat_codex_multi media-send --kind file /absolute/path/to/archive.zip
```

Rules:

- Use a real local file path. The file must exist.
- Prefer absolute paths.
- `media-send` accepts one or more paths. Use this for batch sending instead of running many separate commands.
- Use `--kind` only when every path in the command should be sent as the same type.
- Do not use `--kind image` for mixed batches containing PDFs, videos, or archives.
- Do not write `[[send_image:...]]`, `[[send_file:...]]`, or `[[send_video:...]]` in the final reply unless the command is unavailable.
- Do not mention internal media markers to the user.
- After `media-send` succeeds, answer normally and briefly. The service sends queued media after the Agent turn finishes.
