"""Exercise authentication recovery with a real, local stdio child process."""

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_codex_multi.codex_app_server import AppServerProcess, JsonRpcError
from wechat_codex_multi.codex_cli import CodexCliRunner
from wechat_codex_multi.codex_runtime import CodexAuthError
from wechat_codex_multi.codex_usage import _refresh_codex_auth, read_codex_usage
from wechat_codex_multi.config import DEFAULT_CONFIG
from wechat_codex_multi.service import MultiWechatCodexService
from wechat_codex_multi.state import StateStore


ROUTING_ERROR = "workspace routing discovery unauthorized (401)"
FIXTURE = r'''
import json, os, pathlib, sys
home = pathlib.Path(os.environ["CODEX_HOME"])
scenario = json.loads((home / "scenario.json").read_text())

def count(name):
    path = home / name
    value = int(path.read_text()) + 1 if path.exists() else 1
    path.write_text(str(value))
    return value

def record(method, params):
    with (home / "calls.jsonl").open("a") as out:
        out.write(json.dumps({"method": method, "params": params}) + "\n")

if sys.argv[1] == "app-server":
    for line in sys.stdin:
        req = json.loads(line)
        method, params = req["method"], req.get("params") or {}
        record(method, params)
        if "id" not in req:
            continue
        result, error = {}, ""
        if method == "account/read":
            if params.get("refreshToken"):
                count("refreshes")
                error = scenario.get("refreshError", "")
            result = {"account": None if scenario.get("notLoggedIn") else {"type": "chatgpt"}}
        elif method == "project/list":
            attempt = count("routing-attempts")
            if attempt <= scenario.get("routingFailures", 1):
                error = scenario.get("routingError", "workspace routing discovery unauthorized (401)")
            result = {"data": [{"id": "project-1"}]}
        elif method == "account/rateLimits/read":
            result = {"rateLimits": {"planType": "plus", "primary": {"usedPercent": 7}}}
        response = {"id": req["id"], "error": {"code": -32000, "message": error}} if error else {"id": req["id"], "result": result}
        print(json.dumps(response), flush=True)
else:
    assert "exec" in sys.argv
    record("exec", sys.argv[1:])
    attempt = count("exec-attempts")
    if attempt <= scenario.get("execFailures", 1):
        if scenario.get("toolActivity"):
            print(json.dumps({"type": "item.started", "item": {"type": "command_execution", "id": "command-1"}}))
        print(json.dumps({"type": "error", "message": "workspace routing discovery unauthorized (401)"}))
        sys.exit(1)
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "id": "message-1", "text": "recovered"}}))
'''


class CodexRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "selected-home"
        self.home.mkdir()
        self.bin = self.root / "fake-codex"
        self.bin.write_text(f"#!{sys.executable}\n" + FIXTURE, encoding="utf-8")
        self.bin.chmod(0o700)
        # The selected home must override an unrelated inherited account.
        env = patch.dict(os.environ, {"CODEX_HOME": str(self.root / "wrong-home")})
        env.start()
        self.addCleanup(env.stop)

    def scenario(self, **fields):
        (self.home / "scenario.json").write_text(json.dumps(fields), encoding="utf-8")

    def calls(self):
        return [json.loads(line) for line in (self.home / "calls.jsonl").read_text().splitlines()]

    def server(self):
        server = AppServerProcess(str(self.bin), str(self.home))
        self.addCleanup(server.close)
        return server

    def runner(self):
        config = {"codex": {"bin": str(self.bin), "workingDirectory": str(self.root), "timeoutMs": 5000,
                             "accounts": [{"name": "selected", "codexHome": str(self.home)}]},
                  "media": {"generators": []}}
        state = StateStore(self.root / "state")
        state.update_session("conversation", codexAccount="selected", codexThreadId="existing-thread")
        runner = CodexCliRunner(config, state)
        self.addCleanup(runner.terminate_all)
        return runner, state

    def test_routing_401_refreshes_once_in_same_home_and_retries_request(self):
        self.scenario()
        server = self.server()
        self.assertEqual(server.request("project/list", timeout_s=5), {"data": [{"id": "project-1"}]})
        calls = self.calls()
        self.assertEqual([call["method"] for call in calls],
                         ["initialize", "initialized", "project/list", "account/read", "project/list"])
        self.assertEqual(calls[3]["params"], {"refreshToken": True})

    def test_repeated_routing_401_is_bounded(self):
        self.scenario(routingFailures=99)
        with self.assertRaisesRegex(JsonRpcError, ROUTING_ERROR.replace("(401)", r"\(401\)")):
            self.server().request("project/list", timeout_s=5)
        self.assertEqual((self.home / "routing-attempts").read_text(), "2")
        self.assertEqual((self.home / "refreshes").read_text(), "1")

    def test_revoked_refresh_and_missing_account_do_not_repeat_request(self):
        for fields in ({"refreshError": "refresh token already used"}, {"notLoggedIn": True}):
            with self.subTest(fields=fields):
                self.scenario(routingFailures=99, **fields)
                with self.assertRaisesRegex(JsonRpcError, "令牌刷新失败"):
                    self.server().request("project/list", timeout_s=5)
        self.assertEqual([call["method"] for call in self.calls()].count("project/list"), 2)

    def test_other_errors_are_not_replayed(self):
        self.scenario(routingError="model Unauthorized 401")
        with self.assertRaisesRegex(JsonRpcError, "model Unauthorized 401"):
            self.server().request("project/list", timeout_s=5)
        self.assertFalse((self.home / "refreshes").exists())

    def test_auth_refresh_uses_official_rpc_instead_of_login_status(self):
        self.scenario()
        _refresh_codex_auth(str(self.bin), codex_home=str(self.home), timeout_s=5)
        self.assertEqual([call["method"] for call in self.calls()], ["initialize", "initialized", "account/read"])
        self.assertEqual(self.calls()[-1]["params"], {"refreshToken": True})

    def test_cli_auth_failure_recovers_without_resetting_history(self):
        self.scenario()
        runner, state = self.runner()
        self.assertEqual(runner.run("conversation", "hello"), "recovered")
        self.assertEqual(state.get_session("conversation", str(self.root))["codexThreadId"], "existing-thread")
        self.assertEqual((self.home / "exec-attempts").read_text(), "2")
        self.assertEqual((self.home / "refreshes").read_text(), "1")
        for call in self.calls():
            if call["method"] == "exec":
                args = call["params"]
                self.assertEqual(args[args.index("resume") + 1], "existing-thread")
        self.assertEqual(runner.processes, set())

    def test_cli_does_not_replay_after_tools_execute(self):
        self.scenario(toolActivity=True)
        runner, _state = self.runner()
        with self.assertRaisesRegex(RuntimeError, "workspace routing discovery"):
            runner.run("conversation", "hello")
        self.assertEqual((self.home / "exec-attempts").read_text(), "1")
        self.assertFalse((self.home / "refreshes").exists())

    def test_cli_repeated_401_only_attempts_one_refresh(self):
        self.scenario(execFailures=99)
        runner, state = self.runner()
        with self.assertRaisesRegex(RuntimeError, "workspace routing discovery"):
            runner.run("conversation", "hello")
        self.assertEqual((self.home / "exec-attempts").read_text(), "2")
        self.assertEqual((self.home / "refreshes").read_text(), "1")
        self.assertEqual(state.get_session("conversation", str(self.root))["codexThreadId"], "existing-thread")

    def test_usage_401_refreshes_selected_home_before_retrying_backend(self):
        import urllib.error

        self.scenario()
        (self.home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "fixture"}}))
        with patch("wechat_codex_multi.codex_usage._request_chatgpt_usage",
                   side_effect=[urllib.error.HTTPError("http://fixture", 401, "expired", {}, None), {"ok": True}]) as request:
            self.assertEqual(read_codex_usage(str(self.bin), codex_home=str(self.home)), {"ok": True})
        self.assertEqual(request.call_count, 2)
        self.assertEqual((self.home / "refreshes").read_text(), "1")

    def test_service_reports_correct_account_and_login_command_for_revoked_auth(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["stateDir"] = str(self.root / "service-state")
        config["codex"].update(bin=str(self.bin), workingDirectory=str(self.root),
                               accounts=[{"name": "selected", "codexHome": str(self.home)}], defaultAccount="selected")
        service = MultiWechatCodexService(config)
        self.addCleanup(service.stop)
        sent = []
        service._send_text = lambda _account, _user, message: sent.append(message)
        with patch.object(service, "_handle_message", side_effect=RuntimeError(ROUTING_ERROR)):
            service._handle_message_safe({"accountId": "wechat"}, "user", "wechat:user", "/models")
        self.assertIn("/codex-login selected", sent[-1])
        self.assertIn(str(self.home), sent[-1])
        self.assertIn("login --device-auth", sent[-1])
        self.assertNotIn("wrong-home", sent[-1])

    def test_task_journal_keeps_relogin_instructions_for_background_results(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["stateDir"] = str(self.root / "service-state")
        config["codex"].update(bin=str(self.bin), workingDirectory=str(self.root),
                               accounts=[{"name": "selected", "codexHome": str(self.home)}], defaultAccount="selected")
        service = MultiWechatCodexService(config)
        self.addCleanup(service.stop)
        service._send_text = lambda *_args: None
        with patch.object(service, "_run_codex_and_reply_locked", side_effect=RuntimeError(ROUTING_ERROR)):
            with self.assertRaises(CodexAuthError):
                service._run_codex_and_reply({"accountId": "wechat"}, "user", "wechat:user", "hello")
        task = service.tasks.get(service.task_context.last_task_id)
        self.assertEqual(task["status"], "failed")
        self.assertIn("/codex-login selected", task["error"])
        self.assertIn(str(self.home), task["error"])
