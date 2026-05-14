VALID_AGENTS = {"codex", "claude"}
DEFAULT_AGENT = "codex"


def normalize_agent(value):
    agent = str(value or "").strip().lower()
    aliases = {
        "codex-cli": "codex",
        "codex_cli": "codex",
        "claude-code": "claude",
        "claude_code": "claude",
        "claude-cli": "claude",
        "claude_cli": "claude",
    }
    agent = aliases.get(agent, agent)
    return agent if agent in VALID_AGENTS else ""


def default_agent(config):
    return normalize_agent((config or {}).get("defaultAgent")) or DEFAULT_AGENT


def resolve_session_agent(config, session):
    return normalize_agent((session or {}).get("agent")) or default_agent(config)
