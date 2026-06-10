import copy
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path


DEFAULT_CLAUDE_MODEL_NAMES = ["fable", "sonnet", "opus", "claude-fable-5", "claude-sonnet-4-6"]
DEFAULT_CLAUDE_EFFORT_LEVELS = ["low", "medium", "high", "xhigh"]
DEFAULT_CLAUDE_MODEL_DISCOVERY_TIMEOUT_SECONDS = 5
DEFAULT_CLAUDE_MODEL_DISCOVERY_CACHE_SECONDS = 300
CLAUDE_MODEL_SOURCE_STREAM_JSON = "stream-json"
CLAUDE_EFFORT_SOURCE_CLI_HELP = "cli-help-global"
CLAUDE_EFFORT_SOURCE_FALLBACK = "fallback-global"
CLAUDE_MODEL_ALIASES = {"fable", "opus", "sonnet", "haiku"}
CLAUDE_FULL_MODEL_RE = re.compile(r"\bclaude-[a-z0-9][a-z0-9.-]*\b")
QUOTED_VALUE_RE = re.compile(r"'([^']+)'")
HELP_OPTION_START_RE = re.compile(r"^\s*(?:-\w,\s*)?--[A-Za-z0-9][A-Za-z0-9-]*\b")
SAFE_CLAUDE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_.-]+\])?$")
_MODEL_OPTIONS_CACHE = {}


def default_claude_model_options():
    return [
        {
            "model": model,
            "effort": "",
            "efforts": list(DEFAULT_CLAUDE_EFFORT_LEVELS),
            "effortSource": CLAUDE_EFFORT_SOURCE_FALLBACK,
        }
        for model in DEFAULT_CLAUDE_MODEL_NAMES
    ]


def clear_claude_model_options_cache():
    _MODEL_OPTIONS_CACHE.clear()


def _option_key(option):
    model = str(option.get("model") or "").strip()
    effort = str(option.get("effort") or option.get("reasoningEffort") or "").strip()
    return f"{model}:{effort}" if effort else model


def normalize_claude_model_option(raw, allowed_efforts=None):
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
    effort_levels = set(allowed_efforts or [])
    if effort and effort_levels and effort not in effort_levels:
        return None
    if effort and not SAFE_CLAUDE_TOKEN_RE.match(effort):
        return None
    label = str(option.get("name") or option.get("label") or option.get("display_name") or "").strip()
    normalized = {"model": model, "effort": effort}
    if label:
        normalized["label"] = label
    description = str(option.get("description") or "").strip()
    if description:
        normalized["description"] = description
    group_label = str(option.get("groupLabel") or option.get("group") or "").strip()
    if group_label:
        normalized["groupLabel"] = group_label
    source = str(option.get("modelSource") or option.get("source") or "").strip()
    if source:
        normalized["modelSource"] = source
    efforts = _safe_effort_levels(option.get("efforts") or option.get("availableEfforts") or [])
    if efforts:
        normalized["efforts"] = efforts
    effort_source = str(option.get("effortSource") or "").strip()
    if effort_source:
        normalized["effortSource"] = effort_source
    return normalized


def _safe_effort_levels(raw_levels):
    levels = []
    if isinstance(raw_levels, str):
        raw_levels = _parse_comma_values(raw_levels)
    for raw in raw_levels or []:
        level = str(raw or "").strip()
        if SAFE_CLAUDE_TOKEN_RE.match(level):
            _append_unique(levels, level)
    return levels


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


def _append_unique(values, value):
    if value and value not in values:
        values.append(value)


def _help_option_block(text, option_name):
    lines = (text or "").splitlines()
    block = []
    collecting = False
    option_re = re.compile(rf"^\s*(?:-\w,\s*)?{re.escape(option_name)}\b")
    for line in lines:
        if option_re.search(line):
            block = [line]
            collecting = True
            continue
        if collecting and HELP_OPTION_START_RE.search(line):
            break
        if collecting:
            block.append(line)
    return "\n".join(block)


def _parse_comma_values(text):
    values = []
    for raw in re.split(r"[,|]", text or ""):
        value = raw.strip().strip(" .'\"`")
        if SAFE_CLAUDE_TOKEN_RE.match(value):
            _append_unique(values, value)
    return values


def parse_claude_help_model_names(text):
    names = []
    for value in QUOTED_VALUE_RE.findall(text or ""):
        value = value.strip()
        base_alias = value.split("[", 1)[0]
        if base_alias in CLAUDE_MODEL_ALIASES or value.startswith("claude-"):
            _append_unique(names, value)
    for value in CLAUDE_FULL_MODEL_RE.findall(text or ""):
        _append_unique(names, value.strip())
    return names


def parse_claude_help_effort_levels(text):
    block = _help_option_block(text, "--effort")
    if not block:
        block = text or ""
    for value in re.findall(r"\(([^)]+)\)", block):
        levels = _parse_comma_values(value)
        if levels:
            return levels
    match = re.search(r"Valid values:\s*([A-Za-z0-9_,.\s|\-]+)", block, re.IGNORECASE)
    if match:
        return _parse_comma_values(match.group(1))
    return []


def _expand_optional_path(value):
    value = str(value or "").strip()
    if not value:
        return ""
    return str(Path(os.path.expandvars(os.path.expanduser(value))).resolve())


def _copy_options(options):
    return [copy.deepcopy(option) for option in options]


def _cache_key(claude_bin, claude_config_dir="", cwd=""):
    return (
        shutil.which(claude_bin) or claude_bin,
        _expand_optional_path(claude_config_dir),
        _expand_optional_path(cwd),
    )


def _cached_options(cache_key, cache_ttl_s, now=None):
    if cache_ttl_s <= 0:
        return None
    cached = _MODEL_OPTIONS_CACHE.get(cache_key)
    if not cached:
        return None
    timestamp, options = cached
    now = time.time() if now is None else now
    if now - timestamp > cache_ttl_s:
        _MODEL_OPTIONS_CACHE.pop(cache_key, None)
        return None
    return _copy_options(options)


def _store_cached_options(cache_key, options, cache_ttl_s, now=None):
    if cache_ttl_s <= 0:
        return
    _MODEL_OPTIONS_CACHE[cache_key] = (time.time() if now is None else now, _copy_options(options))


def _extract_stream_json_models(payload):
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("models"), list):
        return payload["models"]
    response = payload.get("response")
    if isinstance(response, dict):
        inner = response.get("response")
        if isinstance(inner, dict) and isinstance(inner.get("models"), list):
            return inner["models"]
        if isinstance(response.get("models"), list):
            return response["models"]
    return []


def _model_group_label(label, description, value):
    title = label or value
    if description:
        return f"{title} — {description}" if title else description
    return title


def _extract_control_response_payload(payload):
    if not isinstance(payload, dict):
        return {}
    response = payload.get("response")
    if isinstance(response, dict) and isinstance(response.get("response"), dict):
        return response["response"]
    return {}


def _stream_payloads_allow_ultracode(payloads):
    for payload in payloads:
        response = _extract_control_response_payload(payload)
        effective = response.get("effective")
        applied = response.get("applied")
        sources = response.get("sources") or []
        settings_values = []
        if isinstance(effective, dict):
            settings_values.append(effective)
        if isinstance(applied, dict):
            settings_values.append(applied)
        for source in sources:
            if isinstance(source, dict) and isinstance(source.get("settings"), dict):
                settings_values.append(source["settings"])
        for settings in settings_values:
            if settings.get("disableWorkflows") is True:
                return False
            if settings.get("enableWorkflows") is False:
                return False
    return True


def parse_claude_stream_json_model_options(payload, include_ultracode=True):
    models = _extract_stream_json_models(payload)
    options = []
    seen = set()
    for raw in models:
        if not isinstance(raw, dict):
            continue
        value = str(raw.get("value") or raw.get("model") or raw.get("id") or "").strip()
        if not value or not SAFE_CLAUDE_TOKEN_RE.match(value):
            continue
        label = str(raw.get("displayName") or raw.get("name") or raw.get("label") or "").strip()
        description = str(raw.get("description") or "").strip()
        efforts = _safe_effort_levels(raw.get("supportedEffortLevels") or raw.get("efforts") or [])
        if raw.get("supportsEffort") is False:
            efforts = []
        if include_ultracode and "xhigh" in efforts and "ultracode" not in efforts:
            efforts.append("ultracode")
        group_label = _model_group_label(label, description, value)
        if not efforts:
            efforts = [""]
        for effort in efforts:
            option = normalize_claude_model_option(
                {
                    "model": value,
                    "effort": effort,
                    "label": label,
                    "description": description,
                    "groupLabel": group_label,
                    "modelSource": CLAUDE_MODEL_SOURCE_STREAM_JSON,
                },
                allowed_efforts=efforts,
            )
            if not option:
                continue
            key = _option_key(option)
            if key in seen:
                continue
            seen.add(key)
            options.append(option)
    return options


def discover_claude_stream_json_model_options(
    claude_bin="claude",
    timeout_s=DEFAULT_CLAUDE_MODEL_DISCOVERY_TIMEOUT_SECONDS,
    claude_config_dir="",
    cwd="",
):
    binary = shutil.which(claude_bin) or claude_bin
    request_id = f"init-{uuid.uuid4().hex[:12]}"
    request = {
        "type": "control_request",
        "request_id": request_id,
        "request": {"subtype": "initialize", "hooks": {}},
    }
    env = os.environ.copy()
    expanded_config_dir = _expand_optional_path(claude_config_dir)
    if expanded_config_dir:
        env["CLAUDE_CONFIG_DIR"] = expanded_config_dir
    completed = subprocess.run(
        [
            binary,
            "--output-format",
            "stream-json",
            "--input-format",
            "stream-json",
            "--verbose",
            "--permission-prompt-tool",
            "stdio",
            "--no-session-persistence",
        ],
        input=json.dumps(request, separators=(",", ":")) + "\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        check=False,
        env=env,
        cwd=_expand_optional_path(cwd) or None,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"claude stream-json 初始化退出码 {completed.returncode}")
    payloads = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        try:
            payloads.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    include_ultracode = _stream_payloads_allow_ultracode(payloads)
    for payload in payloads:
        options = parse_claude_stream_json_model_options(payload, include_ultracode=include_ultracode)
        if options:
            return options
    raise RuntimeError("claude stream-json 初始化未返回 models")


def discover_claude_help_model_options(
    claude_bin="claude",
    timeout_s=DEFAULT_CLAUDE_MODEL_DISCOVERY_TIMEOUT_SECONDS,
):
    binary = shutil.which(claude_bin) or claude_bin
    completed = subprocess.run(
        [binary, "--help"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"claude --help 退出码 {completed.returncode}")
    help_text = f"{completed.stdout}\n{completed.stderr}"
    effort_levels = parse_claude_help_effort_levels(help_text) or DEFAULT_CLAUDE_EFFORT_LEVELS
    options = []
    seen = set()
    for model in parse_claude_help_model_names(help_text):
        option = normalize_claude_model_option({"model": model, "effort": ""})
        if not option:
            continue
        key = _option_key(option)
        if key in seen:
            continue
        option["efforts"] = list(effort_levels)
        option["effortSource"] = CLAUDE_EFFORT_SOURCE_CLI_HELP
        seen.add(key)
        options.append(option)
    return options


def discover_claude_model_options(
    claude_bin="claude",
    timeout_s=DEFAULT_CLAUDE_MODEL_DISCOVERY_TIMEOUT_SECONDS,
    claude_config_dir="",
    cwd="",
):
    return discover_claude_stream_json_model_options(
        claude_bin,
        timeout_s=timeout_s,
        claude_config_dir=claude_config_dir,
        cwd=cwd,
    )


def claude_model_options(config, claude_config_dir="", cwd=""):
    configured = configured_claude_model_options(config)
    if configured:
        return configured
    claude = config.get("claude") or {}
    try:
        timeout_s = int(
            claude.get("modelDiscoveryTimeoutSeconds") or DEFAULT_CLAUDE_MODEL_DISCOVERY_TIMEOUT_SECONDS
        )
        cache_ttl_s = int(
            claude.get("modelDiscoveryCacheSeconds") or DEFAULT_CLAUDE_MODEL_DISCOVERY_CACHE_SECONDS
        )
        key = _cache_key(claude.get("bin") or "claude", claude_config_dir=claude_config_dir, cwd=cwd)
        cached = _cached_options(key, cache_ttl_s)
        if cached:
            return cached
        discovered = discover_claude_model_options(
            claude.get("bin") or "claude",
            timeout_s=timeout_s,
            claude_config_dir=claude_config_dir,
            cwd=cwd,
        )
        if discovered:
            _store_cached_options(key, discovered, cache_ttl_s)
            return discovered
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError):
        pass
    try:
        timeout_s = int(
            claude.get("modelDiscoveryTimeoutSeconds") or DEFAULT_CLAUDE_MODEL_DISCOVERY_TIMEOUT_SECONDS
        )
        discovered = discover_claude_help_model_options(claude.get("bin") or "claude", timeout_s=timeout_s)
        if discovered:
            return discovered
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError):
        pass
    return default_claude_model_options()


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
            str(option.get("label") or ""),
        ]
        if not option.get("effort"):
            keys.append(str(option.get("model") or ""))
        if any(key.lower() == lowered for key in keys if key):
            return dict(option)
    if ":" in value:
        model_part, effort_part = value.rsplit(":", 1)
        model_part = model_part.strip().lower()
        effort_part = effort_part.strip()
        if model_part and effort_part and SAFE_CLAUDE_TOKEN_RE.match(effort_part):
            for option in options:
                if str(option.get("model") or "").lower() != model_part:
                    continue
                efforts = _safe_effort_levels(option.get("efforts") or [])
                if efforts and effort_part not in efforts:
                    continue
                if not efforts and not option.get("effort"):
                    continue
                target = dict(option)
                target["effort"] = effort_part
                return target
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
    if option.get("groupLabel"):
        return key
    return f"{key} ({label})" if label and label != option.get("model") else key


def resolve_session_claude_model(config, session):
    claude = config.get("claude") or {}
    return {
        "model": (session or {}).get("claudeModel") or claude.get("model") or "",
        "effort": (session or {}).get("claudeEffort") or claude.get("effort") or "",
    }
