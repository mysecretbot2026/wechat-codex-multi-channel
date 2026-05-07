CLAUDE_EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}
DEFAULT_CLAUDE_MODEL_NAMES = ["sonnet", "opus", "claude-sonnet-4-6"]
DEFAULT_CLAUDE_EFFORT_LEVELS = ["low", "medium", "high", "xhigh"]


def default_claude_model_options():
    return [
        {"model": model, "effort": effort}
        for model in DEFAULT_CLAUDE_MODEL_NAMES
        for effort in DEFAULT_CLAUDE_EFFORT_LEVELS
    ]


def _option_key(option):
    model = str(option.get("model") or "").strip()
    effort = str(option.get("effort") or option.get("reasoningEffort") or "").strip()
    return f"{model}:{effort}" if effort else model


def normalize_claude_model_option(raw):
    option = dict(raw or {})
    model = str(option.get("model") or option.get("slug") or "").strip()
    if not model:
        return None
    effort = str(
        option.get("effort")
        or option.get("reasoningEffort")
        or option.get("reasoning")
        or option.get("default_reasoning_level")
        or ""
    ).strip()
    if effort and effort not in CLAUDE_EFFORT_LEVELS:
        return None
    label = str(option.get("name") or option.get("label") or option.get("display_name") or "").strip()
    normalized = {"model": model, "effort": effort}
    if label:
        normalized["label"] = label
    return normalized


def configured_claude_model_options(config):
    claude = config.get("claude") or {}
    raw_options = claude.get("modelOptions") or []
    options = []
    seen = set()
    for raw in raw_options:
        option = normalize_claude_model_option(raw)
        if not option:
            continue
        key = _option_key(option)
        if key in seen:
            continue
        seen.add(key)
        options.append(option)
    return options


def claude_model_options(config):
    configured = configured_claude_model_options(config)
    return configured or default_claude_model_options()


def find_claude_model_option(options, selector):
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


def format_claude_model_option(option):
    key = _option_key(option)
    label = str(option.get("label") or "").strip()
    return f"{key} ({label})" if label and label != option.get("model") else key


def resolve_session_claude_model(config, session):
    claude = config.get("claude") or {}
    return {
        "model": (session or {}).get("claudeModel") or claude.get("model") or "",
        "effort": (session or {}).get("claudeEffort") or claude.get("effort") or "",
    }
