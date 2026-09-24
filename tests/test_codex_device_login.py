import base64
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_codex_multi.codex_device_login import CodexDeviceLoginManager
from wechat_codex_multi.config import DEFAULT_CONFIG
from wechat_codex_multi.service import MultiWechatCodexService


class CodexDeviceLoginTests(unittest.TestCase):
    def test_device_login_sends_code_and_checks_email_without_llm(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "backup2"
            home.mkdir()
            fake_bin = Path(tmp) / "codex"
            claims = base64.urlsafe_b64encode(json.dumps({"email": "user@example.com"}).encode()).decode().rstrip("=")
            fake_bin.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys, time\n"
                "home = pathlib.Path(os.environ['CODEX_HOME'])\n"
                "if sys.argv[1:] == ['login', 'status']:\n"
                "    print('Logged in using ChatGPT' if (home / 'auth.json').exists() else 'Not logged in')\n"
                "    sys.exit(0 if (home / 'auth.json').exists() else 1)\n"
                "assert sys.argv[1:] == ['login', '--device-auth']\n"
                "print('https://auth.openai.com/codex/device', flush=True)\n"
                "print('UGLL-K6V9H', flush=True)\n"
                "time.sleep(0.1)\n"
                f"(home / 'auth.json').write_text(json.dumps({{'tokens': {{'id_token': 'x.{claims}.x'}}}}))\n"
            )
            fake_bin.chmod(0o700)
            done = threading.Event()
            codes = []
            results = []
            manager = CodexDeviceLoginManager(str(fake_bin), timeout_seconds=5)
            self.assertTrue(manager.start(str(home), lambda url, code: codes.append((url, code)),
                                          lambda msg: (results.append(msg), done.set()),
                                          expected_email="user@example.com"))
            self.assertFalse(manager.start(str(home), lambda *_: None, lambda *_: None))
            self.assertTrue(done.wait(5))
            self.assertEqual(codes, [("https://auth.openai.com/codex/device", "UGLL-K6V9H")])
            self.assertIn("user@example.com", results[0])
            self.assertIn("登录成功", results[0])
            done.clear()
            self.assertTrue(manager.start(str(home), lambda *_: None,
                                          lambda msg: (results.append(msg), done.set()),
                                          expected_email="someone-else@example.com"))
            self.assertTrue(done.wait(5))
            self.assertIn("与期望的 someone-else@example.com 不符", results[1])
            manager.stop()

    def test_wechat_command_requires_admin_and_selects_configured_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = copy.deepcopy(DEFAULT_CONFIG)
            config["stateDir"] = tmp
            backup2_home = str(Path(tmp) / "backup2")
            config["codex"]["accounts"].append({"name": "backup2", "codexHome": backup2_home})
            config["adminUsers"] = ["admin"]
            service = MultiWechatCodexService(config)
            sent = []
            service._send_text = lambda account, user, msg: sent.append((user, msg))
            account = {"accountId": "acct"}
            key = "acct:admin"
            with patch.object(service.codex_device_login, "start", return_value=True) as start:
                service._handle_message(account, "other", key, "/codex-login backup2 user@example.com")
                start.assert_not_called()
                service._handle_message(account, "admin", key, "/codex-login backup2 user@example.com")
                self.assertEqual(start.call_args.args[0], backup2_home)
                self.assertEqual(start.call_args.kwargs["expected_email"], "user@example.com")
            self.assertIn("只有 adminUsers", sent[0][1])
            self.assertIn("正在为 backup2", sent[1][1])


if __name__ == "__main__":
    unittest.main()
