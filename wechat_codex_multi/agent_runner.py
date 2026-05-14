from .agents import resolve_session_agent
from .claude_cli import ClaudeCliRunner
from .codex_accounts import default_codex_account
from .codex_app_server import CodexAppServerRunner
from .codex_cli import CodexCliRunner


class AgentRunnerManager:
    def __init__(self, config, state_store, codex_factory=None, claude_factory=None):
        self.config = config
        self.state = state_store
        self.codex_factory = codex_factory or self._default_codex_factory
        self.claude_factory = claude_factory or self._default_claude_factory
        self.runners = {}

    @staticmethod
    def _default_codex_factory(config, state_store):
        runner = str(config.get("codex", {}).get("runner") or "exec").strip().lower()
        if runner in {"app-server", "appserver", "server"}:
            return CodexAppServerRunner(config, state_store)
        return CodexCliRunner(config, state_store)

    @staticmethod
    def _default_claude_factory(config, state_store):
        return ClaudeCliRunner(config, state_store)

    def _default_cwd(self):
        return self.config["codex"]["workingDirectory"]

    def _session_agent(self, conversation_key):
        session = self.state.get_session(
            conversation_key,
            self._default_cwd(),
            default_codex_account(self.config),
            self.config.get("defaultAgent") or "codex",
        )
        return resolve_session_agent(self.config, session)

    def runner_for(self, agent):
        name = str(agent or "codex").strip().lower()
        if name not in {"codex", "claude"}:
            name = "codex"
        runner = self.runners.get(name)
        if runner:
            return runner
        if name == "claude":
            runner = self.claude_factory(self.config, self.state)
        else:
            runner = self.codex_factory(self.config, self.state)
        self.runners[name] = runner
        return runner

    def replace_runner(self, agent, runner):
        name = str(agent or "codex").strip().lower()
        old = self.runners.get(name)
        if old:
            try:
                old.terminate_all()
            except Exception:
                pass
        self.runners[name] = runner

    def terminate_agent(self, agent):
        name = str(agent or "codex").strip().lower()
        runner = self.runners.pop(name, None)
        if runner:
            runner.terminate_all()

    def run(self, conversation_key, user_message):
        return self.runner_for(self._session_agent(conversation_key)).run(conversation_key, user_message)

    def steer(self, conversation_key, user_message):
        runner = self.runner_for(self._session_agent(conversation_key))
        if hasattr(runner, "steer"):
            return runner.steer(conversation_key, user_message)
        return False

    def is_running(self, conversation_key):
        return any(runner.is_running(conversation_key) for runner in self.runners.values())

    def cancel(self, conversation_key, reset_session=True):
        for runner in list(self.runners.values()):
            if runner.is_running(conversation_key):
                return runner.cancel(conversation_key, reset_session=reset_session)
        return self.runner_for(self._session_agent(conversation_key)).cancel(conversation_key, reset_session=reset_session)

    def terminate_all(self):
        for runner in list(self.runners.values()):
            runner.terminate_all()
