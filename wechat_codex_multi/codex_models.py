import json
import shutil
import subprocess


REASONING_LEVELS = {"low", "medium", "high", "xhigh"}
DEFAULT_MODEL_NAMES = [
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex",
    "gpt-5.2",
    "codex-auto-review",
]
DEFAULT_REASONING_LEVELS = ["low", "medium", "high", "xhigh"]


def default_model_options():
    return [
        {"model": model, "reasoningEffort": reasoning}
        for model in DEFAULT_MODEL_NAMES
        for reasoning in DEFAULT_REASONING_LEVELS
    ]


def _option_key(option):
    model = str(option.get("model") or "").strip()
    reasoning = str(option.get("reasoningEffort") or "").strip()
    return f"{model}:{reasoning}" if reasoning else model


def normalize_model_option(raw):
    option = dict(raw or {})
    model = str(option.get("model") or option.get("slug") or "").strip()
    if not model:
        return None
    reasoning = str(
        option.get("reasoningEffort")
        or option.get("reasoning")
        or option.get("effort")
        or option.get("default_reasoning_level")
        or ""
    ).strip()
    if reasoning and reasoning not in REASONING_LEVELS:
        return None
    label = str(option.get("name") or option.get("label") or option.get("display_name") or "").strip()
    normalized = {"model": model, "reasoningEffort": reasoning}
    if label:
        normalized["label"] = label
    return normalized


def configured_model_options(config):
    codex = config.get("codex") or {}
    raw_options = codex.get("modelOptions") or []
    options = []
    seen = set()
    for raw in raw_options:
        option = normalize_model_option(raw)
        if not option:
            continue
        key = _option_key(option)
        if key in seen:
            continue
        seen.add(key)
        options.append(option)
    return options


def discover_model_options(codex_bin="codex", timeout_s=10):
    binary = shutil.which(codex_bin) or codex_bin
    completed = subprocess.run(
        [binary, "debug", "models"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"codex debug models 退出码 {completed.returncode}")
    data = json.loads(completed.stdout or "{}")
    options = []
    seen = set()
    for model in data.get("models") or []:
        slug = str(model.get("slug") or "").strip()
        if not slug:
            continue
        levels = model.get("supported_reasoning_levels") or []
        efforts = [str(level.get("effort") or "").strip() for level in levels if isinstance(level, dict)]
        if not efforts:
            efforts = [str(model.get("default_reasoning_level") or "").strip()]
        for effort in efforts:
            option = normalize_model_option(
                {
                    "model": slug,
                    "reasoningEffort": effort,
                    "label": model.get("display_name") or slug,
                }
            )
            if not option:
                continue
            key = _option_key(option)
            if key in seen:
                continue
            seen.add(key)
            options.append(option)
    return options


def model_options(config):
    configured = configured_model_options(config)
    if configured:
        return configured
    codex = config.get("codex") or {}
    return discover_model_options(codex.get("bin") or "codex")


def find_model_option(options, selector):
    value = str(selector or "").strip()
    if not value:
        return None
    if value.isdigit():
        index = int(value) - 1
        if 0 <= index < len(options):
            return dict(options[index])
    lowered = value.lower()
    for option in options:
        keys = [
            _option_key(option),
            str(option.get("model") or ""),
            str(option.get("label") or ""),
        ]
        if any(key.lower() == lowered for key in keys if key):
            return dict(option)
    matches = [
        option
        for option in options
        if _option_key(option).lower().startswith(lowered)
        or str(option.get("label") or "").lower().startswith(lowered)
    ]
    if len(matches) == 1:
        return dict(matches[0])
    return None


def format_model_option(option):
    key = _option_key(option)
    label = str(option.get("label") or "").strip()
    return f"{key} ({label})" if label and label != option.get("model") else key


def resolve_session_model(config, session):
    codex = config.get("codex") or {}
    return {
        "model": (session or {}).get("codexModel") or codex.get("model") or "",
        "reasoningEffort": (session or {}).get("codexReasoningEffort") or codex.get("reasoningEffort") or "",
    }
