"""Opt-in native integration tests; no credentials or live model calls needed.

Run with CODEX_APP_SERVER_TEST_BIN=/path/to/codex python3 -m pytest -q
tests/test_codex_app_server_integration.py. All state lives in a temporary home.
"""

import os
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from wechat_codex_multi.codex_app_server import AppServerProcess, CodexAppServerRunner
from wechat_codex_multi.state import StateStore


def local_responses_provider(home):
    """Route model requests to a loopback fixture instead of a live provider."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            item = {"id": "msg-test", "type": "message", "status": "completed", "role": "assistant",
                    "phase": "final_answer", "content": [{"type": "output_text", "text": "ok", "annotations": []}]}
            events = [
                {"type": "response.created", "response": {"id": "resp-test", "status": "in_progress"}},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": {"id": "resp-test", "status": "completed", "output": [item],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}},
            ]
            data = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    (home / "config.toml").write_text(
        'model_provider = "test"\nmodel = "gpt-5.6-sol"\n'
        '[model_providers.test]\nname = "Local test fixture"\n'
        f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
        'wire_api = "responses"\nrequires_openai_auth = false\n', encoding="utf-8")
    return server, thread, requests


class LocalCommandTurnServer(AppServerProcess):
    """Replace only LLM generation with a local no-op native Codex turn."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.turn_ids = {}

    def _handle_notification(self, message):
        if message.get("method") == "turn/started":
            params = message["params"]
            self.turn_ids[params["threadId"]] = params["turn"]["id"]
        super()._handle_notification(message)

    def request(self, method, params=None, timeout_s=120):
        if method != "turn/start":
            return super().request(method, params, timeout_s)
        thread_id = params["threadId"]
        super().request("thread/shellCommand", {"threadId": thread_id, "command": "true"}, timeout_s)
        context = self.context_for_thread(thread_id)
        if not context.completed.wait(10):
            raise TimeoutError("local command turn did not complete")
        return {"turn": {"id": self.turn_ids[thread_id]}}


@unittest.skipUnless(os.environ.get("CODEX_APP_SERVER_TEST_BIN"), "native Codex smoke test is opt-in")
class NativeWriterReleaseTests(unittest.TestCase):
    def test_native_model_switch_and_inheritance_preserve_thread_and_history(self):
        bin_path = os.environ["CODEX_APP_SERVER_TEST_BIN"]
        with tempfile.TemporaryDirectory(prefix="codex-model-test-") as tmp:
            home = Path(tmp) / "codex-home"
            home.mkdir()
            provider, provider_thread, requests = local_responses_provider(home)
            config = {"codex": {"bin": bin_path, "workingDirectory": tmp, "timeoutMs": 10000,
                                "model": "must-not-override-desktop", "reasoningEffort": "low",
                                "preserveExistingInstructions": True,
                                "accounts": [{"name": "main", "codexHome": str(home)}]},
                      "media": {"generators": []}}
            state = StateStore(Path(tmp) / "bridge-state")
            runner = CodexAppServerRunner(config, state)
            state.update_session("wechat", codexClient="desktop", desktopModelOverride={
                "model": "gpt-5.5", "reasoningEffort": "high",
            })
            workers = []

            def factory(account):
                worker = AppServerProcess(bin_path, account["codexHome"])
                workers.append(worker)
                return worker

            try:
                with patch.object(runner, "_new_run_server", side_effect=factory):
                    runner.run("wechat", "First turn with explicit settings")
                    thread_id = state.get_session("wechat", tmp)["codexThreadId"]
                    catalog = runner._server_for_account(config["codex"]["accounts"][0])

                    def assert_settings(model, effort, turns):
                        saved = catalog.request("thread/read", {"threadId": thread_id, "includeTurns": True})["thread"]
                        self.assertEqual(saved["model"], model)
                        self.assertEqual(saved["reasoningEffort"], effort)
                        self.assertEqual(len(saved["turns"]), turns)
                        self.assertEqual(state.get_session("wechat", tmp)["codexThreadId"], thread_id)

                    assert_settings("gpt-5.5", "high", 1)
                    self.assertFalse(state.get_session("wechat", tmp)["desktopModelOverride"])
                    runner.run("wechat", "Inherit previous settings")
                    assert_settings("gpt-5.5", "high", 2)

                    state.update_session("wechat", desktopModelOverride={"model": "gpt-5.6-sol", "reasoningEffort": "medium"})
                    runner.run("wechat", "Change model, keep history")
                    assert_settings("gpt-5.6-sol", "medium", 3)

                    # Simulate the same native thread being changed in another client.
                    state.update_session("desktop", codexClient="desktop", codexThreadId=thread_id,
                                         desktopModelOverride={"model": "gpt-5.5", "reasoningEffort": "low"})
                    runner.run("desktop", "Desktop-side model change")
                    runner.run("wechat", "Use the latest native settings, not stale cache")
                    assert_settings("gpt-5.5", "low", 5)
                    # Some builds issue additional title-generation requests.
                    transitions = []
                    for request in requests:
                        if not transitions or transitions[-1] != request["model"]:
                            transitions.append(request["model"])
                    self.assertGreaterEqual(len(requests), 5)
                    self.assertEqual(transitions, ["gpt-5.5", "gpt-5.6-sol", "gpt-5.5"])
                    self.assertTrue(all(worker.process.poll() is not None for worker in workers))
            finally:
                runner.terminate_all()
                for worker in workers:
                    worker.close()
                provider.shutdown()
                provider.server_close()
                provider_thread.join(2)

    def test_completed_turn_hands_off_immediately_and_preserves_history(self):
        bin_path = os.environ["CODEX_APP_SERVER_TEST_BIN"]
        with tempfile.TemporaryDirectory(prefix="codex-writer-test-") as tmp:
            home = Path(tmp) / "codex-home"
            home.mkdir()
            config = {"codex": {"bin": bin_path, "workingDirectory": tmp, "timeoutMs": 10000,
                                "preserveExistingInstructions": True, "accounts": [
                                    {"name": "main", "codexHome": str(home)},
                                ]}, "media": {"generators": []}}
            state = StateStore(Path(tmp) / "bridge-state")
            runner = CodexAppServerRunner(config, state)
            desktop = AppServerProcess(bin_path, str(home))
            workers = []

            def factory(account):
                worker = LocalCommandTurnServer(bin_path, account["codexHome"])
                workers.append(worker)
                return worker

            try:
                with patch.object(runner, "_new_run_server", side_effect=factory):
                    runner.run("wechat", "Writer release smoke test")
                    thread_id = state.get_session("wechat", tmp)["codexThreadId"]
                    self.assertIsNotNone(workers[-1].process.poll())

                    # A genuinely separate server acquires the writer immediately.
                    resumed = desktop.request("thread/resume", {"threadId": thread_id}, timeout_s=15)
                    self.assertEqual(resumed["thread"]["id"], thread_id)
                    self.assertEqual(len(resumed["thread"]["turns"]), 1)
                    self.assertEqual(resumed["thread"]["turns"][-1]["status"], "completed")

                    # While that server owns it, WeChat must not steal or replace it.
                    with self.assertRaisesRegex(RuntimeError, "active writer"):
                        runner.run("wechat", "Must not run while desktop owns the thread")
                    self.assertEqual(state.get_session("wechat", tmp)["codexThreadId"], thread_id)
                    desktop.close()

                    runner.run("wechat", "Continue the same thread")
                    self.assertEqual(state.get_session("wechat", tmp)["codexThreadId"], thread_id)
                    catalog = runner._server_for_account(config["codex"]["accounts"][0])
                    saved = catalog.request("thread/read", {"threadId": thread_id, "includeTurns": True})
                    self.assertEqual(len(saved["thread"]["turns"]), 2)
                    self.assertEqual(saved["thread"]["turns"][-1]["status"], "completed")
                    self.assertEqual(catalog.request("thread/loaded/list", {})["data"], [])
                    self.assertEqual(runner.run_servers, {})
                    self.assertTrue(all(worker.process.poll() is not None for worker in workers))
            finally:
                desktop.close()
                runner.terminate_all()
                for worker in workers:
                    worker.close()


if __name__ == "__main__":
    unittest.main()
