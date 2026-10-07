import tempfile
import threading
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from wechat_codex_multi.codex_app_server import AppServerProcess, AppTurnState, CodexAppServerRunner
from wechat_codex_multi.codex_cli import CodexCancelled


class FakeState:
    def __init__(self):
        self.lock = threading.RLock()
        self.updates = []
        self.reset_calls = []
        self.sessions = {}

    def get_session(self, conversation_key, default_cwd, default_account):
        return dict(self.sessions.setdefault(conversation_key, {"cwd": default_cwd, "codexAccount": default_account}))

    def update_session(self, conversation_key, **updates):
        self.updates.append((conversation_key, updates))
        self.sessions.setdefault(conversation_key, {}).update(updates)

    def reset_session(self, conversation_key):
        self.reset_calls.append(conversation_key)
        self.sessions.setdefault(conversation_key, {})["codexThreadId"] = ""


class FakeServer:
    def __init__(self):
        self.requests = []
        self.registered = []
        self.unregistered = []

    def request(self, method, params=None, timeout_s=120):
        self.requests.append((method, params or {}, timeout_s))
        if method == "thread/start":
            return {"thread": {"id": "thread-new"}}
        return {}

    def register_context(self, context):
        self.registered.append(context)

    def unregister_context(self, context):
        self.unregistered.append(context)


class FailingResumeServer(FakeServer):
    def request(self, method, params=None, timeout_s=120):
        if method == "thread/resume":
            self.requests.append((method, params or {}, timeout_s))
            raise RuntimeError("resume unavailable")
        return super().request(method, params, timeout_s)


class MissingPendingThreadServer(FakeServer):
    def request(self, method, params=None, timeout_s=120):
        if method == "thread/resume":
            self.requests.append((method, params or {}, timeout_s))
            raise RuntimeError("no rollout found for thread id old-pending")
        return super().request(method, params, timeout_s)


class TurnServer(AppServerProcess):
    """Exercise the real notification/context routing without launching Codex."""

    def __init__(self, outcome="completed", resume_error="", on_request=None, native_settings=None):
        super().__init__("codex", str(Path.home() / ".codex"))
        self.outcome = outcome
        self.resume_error = resume_error
        self.on_request = on_request
        self.requests = []
        self.close_calls = 0
        self.turn_started = threading.Event()
        self.closed_event = threading.Event()
        self.native_settings = native_settings or {}

    def start(self):
        if self.closed:
            raise RuntimeError("app-server stopped")

    def close(self):
        self.close_calls += 1
        self.closed = True
        for context in list(self.contexts_by_thread.values()):
            if context.running:
                context.finish("failed", "app-server stopped")
        self.closed_event.set()

    def complete(self, thread_id, status="completed"):
        self._handle_notification({"method": "item/completed", "params": {
            "threadId": thread_id, "item": {"id": "message-1", "type": "agentMessage", "text": "测试完成"},
        }})
        self._handle_notification({"method": "turn/completed", "params": {
            "threadId": thread_id, "turn": {"id": "turn-1", "status": status,
                                           "error": {"message": "model failed"} if status == "failed" else None},
        }})

    def request(self, method, params=None, timeout_s=120):
        self.start()
        params = params or {}
        self.requests.append((method, params, timeout_s))
        if self.on_request:
            self.on_request(method, params)
        if method == "thread/resume" and self.resume_error:
            raise RuntimeError(self.resume_error)
        if method == "thread/start":
            return {"thread": {"id": "thread-new"}, **self.native_settings}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"]}, **self.native_settings}
        if method == "turn/start":
            context = self.context_for_thread(params["threadId"])
            with context.lock:
                context.active_turn_id = "turn-1"
            self.turn_started.set()
            if self.outcome == "start_error":
                raise RuntimeError("turn start failed")
            if self.outcome == "missing_id":
                return {"turn": {}}
            if self.outcome != "pending":
                self.complete(params["threadId"], self.outcome)
            return {"turn": {"id": "turn-1"}}
        if method == "turn/interrupt":
            self.complete(params["threadId"], "interrupted")
        return {}


def make_config(tmp):
    return {
        "codex": {
            "bin": "codex",
            "workingDirectory": tmp,
            "timeoutMs": 1000,
            "bypassApprovalsAndSandbox": True,
        },
        "media": {"generators": []},
    }


class CodexAppServerPromptVersionTests(unittest.TestCase):
    def test_authentication_failure_does_not_reset_cli_thread_or_create_new_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = FakeState()
            runner = CodexAppServerRunner(make_config(tmp), state)
            server = Mock()
            server.request.side_effect = RuntimeError("workspace routing discovery unauthorized (401)")
            context = runner._context("conversation-1", "thread-1")
            with self.assertRaisesRegex(RuntimeError, "workspace routing discovery"):
                runner._ensure_thread(server, context, "conversation-1", {"codexThreadId": "thread-1"},
                                      tmp, "", "", "main", "version", "instructions")
            self.assertEqual(state.reset_calls, [])
            self.assertEqual([call.args[0] for call in server.request.call_args_list], ["thread/resume"])

    def test_resume_with_current_prompt_version_omits_base_instructions(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = CodexAppServerRunner(make_config(tmp), FakeState())
            server = FakeServer()
            context = runner._context("conversation-1", "thread-1")
            instructions = runner._instructions()
            version = runner._prompt_version(instructions)
            session = {"codexThreadId": "thread-1", "codexAppServerPromptVersion": version}

            thread_id = runner._ensure_thread(
                server, context, "conversation-1", session, tmp, "", "", "main", version, instructions
            )

            self.assertEqual(thread_id, "thread-1")
            method, params, _timeout = server.requests[0]
            self.assertEqual(method, "thread/resume")
            self.assertNotIn("baseInstructions", params)
            self.assertNotIn("developerInstructions", params)

    def test_resume_with_old_prompt_version_includes_base_instructions(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = CodexAppServerRunner(make_config(tmp), FakeState())
            server = FakeServer()
            context = runner._context("conversation-1", "thread-1")
            instructions = runner._instructions()
            version = runner._prompt_version(instructions)
            session = {"codexThreadId": "thread-1", "codexAppServerPromptVersion": "old"}

            thread_id = runner._ensure_thread(
                server, context, "conversation-1", session, tmp, "", "", "main", version, instructions
            )

            self.assertEqual(thread_id, "thread-1")
            method, params, _timeout = server.requests[0]
            self.assertEqual(method, "thread/resume")
            self.assertEqual(params["baseInstructions"], instructions)
            self.assertEqual(params["developerInstructions"], "")

    def test_new_thread_records_prompt_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = FakeState()
            runner = CodexAppServerRunner(make_config(tmp), state)
            server = FakeServer()
            context = runner._context("conversation-1", "")
            instructions = runner._instructions()
            version = runner._prompt_version(instructions)
            session = {"codexThreadId": "", "desktopProjectId": "native-project-1"}

            thread_id = runner._ensure_thread(
                server, context, "conversation-1", session, tmp, "", "", "main", version, instructions
            )

            self.assertEqual(thread_id, "thread-new")
            method, params, _timeout = server.requests[0]
            self.assertEqual(method, "thread/start")
            self.assertEqual(params["projectId"], "native-project-1")
            self.assertEqual(params["threadSource"], "user")
            self.assertEqual(params["baseInstructions"], instructions)
            self.assertEqual(state.updates[-1][1]["codexAppServerPromptVersion"], version)

    def test_new_thread_is_named_for_desktop_catalog_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = FakeState()
            runner = CodexAppServerRunner(make_config(tmp), state)
            server = FakeServer()
            context = runner._context("conversation-1", "")
            instructions = runner._instructions()
            version = runner._prompt_version(instructions)

            runner._ensure_thread(
                server, context, "conversation-1",
                {"codexThreadId": "", "desktopProjectId": "native-project-1"},
                tmp, "", "", "main", version, instructions,
                "第一条任务\n" + "很长" * 100,
            )

            self.assertEqual([method for method, _, _ in server.requests],
                             ["thread/start", "thread/name/set"])
            _method, params, _timeout = server.requests[-1]
            self.assertEqual(params["threadId"], "thread-new")
            self.assertTrue(params["name"].startswith("第一条任务"))
            self.assertLessEqual(len(params["name"]), 80)

    def test_desktop_resume_failure_keeps_selected_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            config["codex"]["preserveExistingInstructions"] = True
            state = FakeState()
            runner = CodexAppServerRunner(config, state)
            server = FailingResumeServer()
            context = runner._context("conversation-1", "thread-1")
            instructions = runner._instructions()
            version = runner._prompt_version(instructions)
            with self.assertRaisesRegex(RuntimeError, "无法续聊所选桌面会话"):
                runner._ensure_thread(
                    server, context, "conversation-1", {"codexThreadId": "thread-1"},
                    tmp, "", "", "main", version, instructions,
                )
            self.assertEqual([method for method, _, _ in server.requests], ["thread/resume"])
            self.assertEqual(state.reset_calls, [])

    def test_missing_pending_desktop_thread_starts_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            config["codex"]["preserveExistingInstructions"] = True
            state = FakeState()
            runner = CodexAppServerRunner(config, state)
            server = MissingPendingThreadServer()
            context = runner._context("conversation-1", "old-pending")
            instructions = runner._instructions()
            version = runner._prompt_version(instructions)
            result = runner._ensure_thread(
                server, context, "conversation-1",
                {"codexThreadId": "old-pending", "desktopPendingThread": True},
                tmp, "", "", "main", version, instructions,
            )
            self.assertEqual(result, "thread-new")
            self.assertEqual([method for method, _, _ in server.requests], ["thread/resume", "thread/start"])


class CodexAppServerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = FakeState()
        self.state.sessions["conversation-1"] = {"codexThreadId": "thread-1"}
        self.runner = CodexAppServerRunner(make_config(self.tmp.name), self.state)
        self.runner.timeout_ms = 5000
        # Catalog connections must never be used to execute, steer or interrupt.
        self.catalog_patch = patch.object(self.runner, "_server_for_account", side_effect=AssertionError("catalog used"))
        self.catalog_patch.start()
        self.addCleanup(self.catalog_patch.stop)

    def run_in_background(self, key):
        results, errors = [], []

        def run():
            try:
                results.append(self.runner.run(key, "测试任务"))
            except Exception as err:
                errors.append(err)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.runner.cancel, key, False)
        return thread, results, errors

    def assert_released(self, server, key="conversation-1"):
        self.assertTrue(server.closed)
        self.assertEqual(server.close_calls, 1)
        self.assertEqual(server.contexts_by_thread, {})
        self.assertNotIn(key, self.runner.run_servers)
        self.assertFalse(self.runner.is_running(key))

    def test_desktop_resume_inherits_native_model_and_records_effective_settings(self):
        self.runner.config["codex"].update(model="wrong-global", reasoningEffort="low")
        self.state.sessions["conversation-1"].update(codexClient="desktop", codexModel="stale", codexReasoningEffort="low")
        server = TurnServer(native_settings={"model": "gpt-native", "reasoningEffort": "xhigh"})
        with patch.object(self.runner, "_new_run_server", return_value=server):
            self.runner.run("conversation-1", "test")
        for method, params, _ in server.requests:
            if method in {"thread/resume", "turn/start"}:
                self.assertNotIn("model", params)
                self.assertNotIn("effort", params)
                self.assertNotIn("config", params)
        saved = self.state.sessions["conversation-1"]
        self.assertEqual((saved["codexModel"], saved["codexReasoningEffort"]), ("gpt-native", "xhigh"))

    def test_desktop_explicit_model_reaches_resume_and_turn_without_reset(self):
        override = {"model": "gpt-chosen", "reasoningEffort": "high"}
        self.state.sessions["conversation-1"].update(codexClient="desktop", desktopModelOverride=override)
        server = TurnServer(native_settings=override)
        with patch.object(self.runner, "_new_run_server", return_value=server):
            self.runner.run("conversation-1", "test")
        resume, turn = server.requests[:2]
        self.assertEqual(resume[1]["model"], "gpt-chosen")
        self.assertEqual(resume[1]["config"], {"model_reasoning_effort": "high"})
        self.assertNotIn("effort", resume[1])
        self.assertEqual(turn[1]["model"], "gpt-chosen")
        self.assertEqual(turn[1]["effort"], "high")
        self.assertEqual(self.state.sessions["conversation-1"]["desktopModelOverride"], {})
        self.assertEqual(self.state.sessions["conversation-1"]["codexThreadId"], "thread-1")

    def test_model_command_received_mid_turn_is_not_erased(self):
        old = {"model": "gpt-old", "reasoningEffort": "low"}
        new = {"model": "gpt-new", "reasoningEffort": "high"}
        self.state.sessions["conversation-1"].update(codexClient="desktop", desktopModelOverride=old)
        server = TurnServer(on_request=lambda method, _: self.state.update_session("conversation-1", desktopModelOverride=new)
                            if method == "turn/start" else None, native_settings=old)
        with patch.object(self.runner, "_new_run_server", return_value=server):
            self.runner.run("conversation-1", "test")
        saved = self.state.sessions["conversation-1"]
        self.assertEqual(saved["desktopModelOverride"], new)
        self.assertEqual(saved["codexModel"], "gpt-old")

    def test_rejected_turn_keeps_pending_model_for_retry(self):
        override = {"model": "gpt-chosen", "reasoningEffort": "high"}
        self.state.sessions["conversation-1"].update(codexClient="desktop", desktopModelOverride=override)
        server = TurnServer("start_error", native_settings=override)
        with patch.object(self.runner, "_new_run_server", return_value=server):
            with self.assertRaisesRegex(RuntimeError, "turn start failed"):
                self.runner.run("conversation-1", "test")
        self.assertEqual(self.state.sessions["conversation-1"]["desktopModelOverride"], override)
        self.assert_released(server)

    def test_success_releases_before_return_and_next_turn_resumes_same_thread(self):
        first, second = TurnServer(), TurnServer()
        with patch.object(self.runner, "_new_run_server", side_effect=[first, second]):
            self.assertEqual(self.runner.run("conversation-1", "第一轮"), "测试完成")
            self.assert_released(first)
            self.assertEqual(self.runner.run("conversation-1", "第二轮"), "测试完成")
        self.assert_released(second)
        for server in (first, second):
            self.assertEqual([m for m, _, _ in server.requests], ["thread/resume", "turn/start"])
            self.assertEqual(server.requests[0][1]["threadId"], "thread-1")
        self.assertEqual(self.state.sessions["conversation-1"]["codexThreadId"], "thread-1")

    def test_new_thread_is_persisted_and_released(self):
        server = TurnServer()
        self.state.sessions["conversation-1"] = {"desktopProjectId": "project-1"}
        with patch.object(self.runner, "_new_run_server", return_value=server):
            self.runner.run("conversation-1", "新建项目任务")
        self.assert_released(server)
        self.assertEqual(self.state.sessions["conversation-1"]["codexThreadId"], "thread-new")
        self.assertEqual(server.requests[0][1]["projectId"], "project-1")

    def test_turn_failures_always_release_and_keep_thread(self):
        for outcome, message in (("failed", "model failed"), ("start_error", "turn start failed"),
                                 ("missing_id", "did not return turn id")):
            with self.subTest(outcome=outcome):
                server = TurnServer(outcome)
                with patch.object(self.runner, "_new_run_server", return_value=server):
                    with self.assertRaisesRegex(RuntimeError, message):
                        self.runner.run("conversation-1", "测试失败")
                self.assert_released(server)
                self.assertEqual(self.state.sessions["conversation-1"]["codexThreadId"], "thread-1")

    def test_initialize_failure_closes_worker(self):
        server = TurnServer()
        with patch.object(server, "start", side_effect=RuntimeError("initialize failed")), \
                patch.object(self.runner, "_new_run_server", return_value=server):
            with self.assertRaisesRegex(RuntimeError, "initialize failed"):
                self.runner.run("conversation-1", "test")
        self.assert_released(server)

    def test_state_write_failure_after_thread_start_releases_worker(self):
        server = TurnServer()
        self.state.sessions["conversation-1"] = {}
        with patch.object(self.runner, "_new_run_server", return_value=server), \
                patch.object(self.state, "update_session", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.runner.run("conversation-1", "test")
        self.assert_released(server)

    def test_writer_conflict_preserves_selection_without_starting_replacement(self):
        for preserve in (False, True):
            with self.subTest(preserve=preserve):
                self.runner.preserve_existing_instructions = preserve
                server = TurnServer(resume_error="thread thread-1 already has an active writer")
                with patch.object(self.runner, "_new_run_server", return_value=server):
                    with self.assertRaisesRegex(RuntimeError, "已保留所选会话，不会另建会话"):
                        self.runner.run("conversation-1", "test")
                self.assert_released(server)
                self.assertEqual([m for m, _, _ in server.requests], ["thread/resume"])
                self.assertEqual(self.state.reset_calls, [])
                self.assertEqual(self.state.sessions["conversation-1"]["codexThreadId"], "thread-1")

    def test_timeout_interrupts_and_releases_without_resetting_selection(self):
        self.runner.timeout_ms = 1
        server = TurnServer("pending")
        with patch.object(self.runner, "_new_run_server", return_value=server):
            with self.assertRaisesRegex(RuntimeError, "没有返回结果"):
                self.runner.run("conversation-1", "test")
        self.assert_released(server)
        self.assertEqual(server.requests[-1][0], "turn/interrupt")
        self.assertEqual(self.state.reset_calls, [])

    def test_interrupt_and_reset_release_only_the_active_worker(self):
        for reset in (False, True):
            with self.subTest(reset=reset):
                self.state.sessions["conversation-1"] = {"codexThreadId": "thread-1"}
                server = TurnServer("pending")
                with patch.object(self.runner, "_new_run_server", return_value=server):
                    thread, _, errors = self.run_in_background("conversation-1")
                    self.assertTrue(server.turn_started.wait(2))
                    self.assertTrue(self.runner.cancel("conversation-1", reset_session=reset))
                    thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertIsInstance(errors[0], CodexCancelled)
                self.assert_released(server)
                self.assertEqual(self.state.sessions["conversation-1"]["codexThreadId"], "" if reset else "thread-1")

    def test_reset_during_thread_start_does_not_restore_cancelled_selection(self):
        self.state.sessions["conversation-1"] = {}
        server = TurnServer(on_request=lambda method, _: self.runner.cancel("conversation-1")
                            if method == "thread/start" else None)
        with patch.object(self.runner, "_new_run_server", return_value=server):
            with self.assertRaises(CodexCancelled):
                self.runner.run("conversation-1", "test")
        self.assert_released(server)
        self.assertEqual(self.state.sessions["conversation-1"]["codexThreadId"], "")
        self.assertNotIn("turn/start", [m for m, _, _ in server.requests])

    def test_cancel_racing_turn_start_still_closes_worker(self):
        server = TurnServer("pending", on_request=lambda method, _: self.runner.cancel("conversation-1", False)
                            if method == "turn/start" else None)
        with patch.object(self.runner, "_new_run_server", return_value=server):
            with self.assertRaises(CodexCancelled):
                self.runner.run("conversation-1", "test")
        self.assert_released(server)

    def test_parallel_threads_are_isolated_and_steer_uses_execution_worker(self):
        first, second, catalog = TurnServer("pending"), TurnServer("pending"), TurnServer()
        self.runner.servers["catalog"] = catalog
        self.state.sessions["conversation-2"] = {"codexThreadId": "thread-2"}
        with patch.object(self.runner, "_new_run_server", side_effect=[first, second]):
            a, results_a, errors_a = self.run_in_background("conversation-1")
            self.assertTrue(first.turn_started.wait(2))
            b, results_b, errors_b = self.run_in_background("conversation-2")
            self.assertTrue(second.turn_started.wait(2))
            self.assertEqual(len(self.runner.active_runs()), 2)
            self.assertTrue(self.runner.steer("conversation-2", "补充内容"))
            self.assertEqual(second.requests[-1][0], "turn/steer")
            first.complete("thread-1")
            a.join(2)
            self.assert_released(first)
            self.assertFalse(second.closed)
            self.assertTrue(self.runner.is_running("conversation-2"))
            self.assertFalse(catalog.closed)
            second.complete("thread-2")
            b.join(2)
        self.assert_released(second, "conversation-2")
        self.assertEqual(errors_a + errors_b, [])
        self.assertEqual(results_a + results_b, ["测试完成", "测试完成"])

    def test_same_thread_cannot_run_twice_or_be_closed_by_duplicate_run(self):
        server = TurnServer("pending")
        self.state.sessions["conversation-2"] = {"codexThreadId": "thread-1"}
        with patch.object(self.runner, "_new_run_server", return_value=server) as factory:
            thread, _, errors = self.run_in_background("conversation-1")
            self.assertTrue(server.turn_started.wait(2))
            for key in ("conversation-1", "conversation-2"):
                with self.assertRaisesRegex(RuntimeError, "已有运行中任务"):
                    self.runner.run(key, "test")
            self.assertEqual(factory.call_count, 1)
            self.assertFalse(server.closed)
            server.complete("thread-1")
            thread.join(2)
        self.assertEqual(errors, [])
        self.assert_released(server)

    def test_idle_steer_cancel_do_not_acquire_thread_or_launch_server(self):
        with patch.object(self.runner, "_new_run_server", side_effect=AssertionError("worker created")):
            self.assertFalse(self.runner.steer("conversation-1", "test"))
            self.assertFalse(self.runner.cancel("conversation-1", reset_session=False))
        self.assertEqual(self.state.sessions["conversation-1"]["codexThreadId"], "thread-1")

    def test_thread_stays_reserved_until_worker_exit_finishes(self):
        server = TurnServer("pending")
        closing, allow_close = threading.Event(), threading.Event()
        original_close = server.close

        def delayed_close():
            closing.set()
            allow_close.wait(3)
            original_close()

        with patch.object(server, "close", side_effect=delayed_close), \
                patch.object(self.runner, "_new_run_server", return_value=server):
            thread, _, errors = self.run_in_background("conversation-1")
            self.addCleanup(allow_close.set)
            try:
                self.assertTrue(server.turn_started.wait(2))
                server.complete("thread-1")
                self.assertTrue(closing.wait(2))
                self.assertTrue(self.runner.is_running("conversation-1"))
                with self.assertRaisesRegex(RuntimeError, "正在释放占用"):
                    self.runner.run("conversation-1", "test")
            finally:
                allow_close.set()
                thread.join(2)
        self.assertEqual(errors, [])
        self.assert_released(server)

    def test_failed_interrupt_still_closes_worker_on_timeout(self):
        def reject_interrupt(method, _):
            if method == "turn/interrupt":
                raise TimeoutError("interrupt failed")

        server = TurnServer("pending", on_request=reject_interrupt)
        self.runner.timeout_ms = 1
        with patch.object(self.runner, "_new_run_server", return_value=server):
            with self.assertRaisesRegex(RuntimeError, "没有返回结果"):
                self.runner.run("conversation-1", "test")
        self.assert_released(server)
        self.assertEqual(self.state.sessions["conversation-1"]["codexThreadId"], "thread-1")

    def test_terminate_all_closes_catalog_and_wakes_active_run(self):
        server, catalog = TurnServer("pending"), TurnServer()
        self.runner.servers["catalog"] = catalog
        with patch.object(self.runner, "_new_run_server", return_value=server):
            thread, _, errors = self.run_in_background("conversation-1")
            self.assertTrue(server.turn_started.wait(2))
            self.runner.terminate_all()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(catalog.closed)
        self.assertTrue(server.closed)
        self.assertFalse(self.runner.is_running("conversation-1"))
        self.assertRegex(str(errors[0]), "app-server stopped")


class AppServerProcessLifecycleTests(unittest.TestCase):
    def test_process_exit_wakes_running_context_and_pending_requests(self):
        server = AppServerProcess("codex")
        server.process = SimpleNamespace(stdout=StringIO(""))
        context = AppTurnState("conversation-1", "thread-1")
        context.start_turn("turn-1")
        server.register_context(context)
        slot = {"event": threading.Event(), "response": None}
        server.pending[1] = slot
        server._read_loop()
        self.assertTrue(context.completed.is_set())
        self.assertEqual(context.status, "failed")
        self.assertEqual(context.error, "app-server stopped")
        self.assertTrue(slot["event"].is_set())
        self.assertTrue(server.closed)

    def test_closed_worker_cannot_be_restarted_by_late_interrupt_or_steer(self):
        server = AppServerProcess("codex")
        server.close()
        with patch("wechat_codex_multi.codex_app_server.subprocess.Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "app-server stopped"):
                server.request("turn/interrupt", {"threadId": "thread-1", "turnId": "turn-1"})
        popen.assert_not_called()

    def test_unregister_removes_mapping_even_after_context_thread_id_changed(self):
        server = AppServerProcess("codex")
        context = AppTurnState("conversation-1", "thread-1")
        server.register_context(context)
        context.thread_id = ""
        server.unregister_context(context)
        self.assertEqual(server.contexts_by_thread, {})


if __name__ == "__main__":
    unittest.main()
