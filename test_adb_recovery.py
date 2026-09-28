import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from perfpilot.runtime import _clear_stale_adb_listener, restart_adb_server
from perfpilot.server import Handler, _adb_restart_lock


class AdbRecoveryTests(unittest.TestCase):
    @patch("perfpilot.runtime.adb_env", return_value={})
    @patch("perfpilot.runtime._clear_stale_adb_listener")
    @patch("perfpilot.runtime.Path.is_file", return_value=True)
    @patch("perfpilot.runtime.adb_executable", return_value="adb")
    @patch("perfpilot.runtime.subprocess.run")
    def test_restart_order_and_error(self, run, *_):
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        restart_adb_server()
        self.assertEqual([call.args[0][1] for call in run.call_args_list], ["kill-server", "start-server", "devices"])
        run.reset_mock()
        run.side_effect = [subprocess.CompletedProcess([], 0, "", ""), subprocess.CompletedProcess([], 1, "", "port busy")]
        with self.assertRaisesRegex(RuntimeError, "port busy"):
            restart_adb_server()
        self.assertEqual(run.call_count, 2)
        run.reset_mock()
        run.side_effect = [subprocess.CompletedProcess([], 1, "", "stale listener"),
                           subprocess.CompletedProcess([], 0, "", ""), subprocess.CompletedProcess([], 0, "", "")]
        restart_adb_server()
        self.assertEqual(run.call_count, 3)

    @patch("perfpilot.runtime.subprocess.run")
    def test_stale_listener_only_kills_verified_adb(self, run):
        listener = "  TCP    127.0.0.1:5037    0.0.0.0:0    LISTENING    42\n"
        run.side_effect = [
            subprocess.CompletedProcess([], 0, listener, ""),
            subprocess.CompletedProcess([], 0, '"other.exe","42","Console","1","100 K"\n', ""),
        ]
        with self.assertRaisesRegex(RuntimeError, "未结束该进程"):
            _clear_stale_adb_listener()
        self.assertEqual(run.call_count, 2)

        run.reset_mock()
        run.side_effect = [
            subprocess.CompletedProcess([], 0, listener, ""),
            subprocess.CompletedProcess([], 0, '"adb.exe","42","Console","1","100 K"\n', ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        _clear_stale_adb_listener()
        self.assertEqual(run.call_args_list[-1].args[0], ["taskkill", "/PID", "42", "/F"])

    def test_api_rejects_active_android_monitor(self):
        handler = Handler.__new__(Handler)
        handler.path = "/api/v1/adb/restart"
        handler.headers = {}
        handler.server = SimpleNamespace(server_port=8765)
        with patch.object(handler, "read_json", return_value={}), patch.object(handler, "send_json") as send, \
                patch("perfpilot.server.manager.active", return_value=[{"device": {"platform": "android"}}]), \
                patch("perfpilot.server.restart_adb_server") as restart:
            handler.do_POST()
        restart.assert_not_called()
        self.assertEqual(send.call_args.args[1], 409)

    def test_api_rejects_concurrent_restart(self):
        handler = Handler.__new__(Handler)
        handler.path = "/api/v1/adb/restart"
        handler.headers = {}
        handler.server = SimpleNamespace(server_port=8765)
        _adb_restart_lock.acquire()
        try:
            with patch.object(handler, "read_json", return_value={}), patch.object(handler, "send_json") as send, \
                    patch("perfpilot.server.restart_adb_server") as restart:
                handler.do_POST()
            restart.assert_not_called()
            self.assertEqual(send.call_args.args[1], 409)
        finally:
            _adb_restart_lock.release()

    def test_api_rejects_other_origin(self):
        handler = Handler.__new__(Handler)
        handler.path = "/api/v1/adb/restart"
        handler.headers = {"Origin": "https://example.org"}
        handler.server = SimpleNamespace(server_port=8765)
        with patch.object(handler, "read_json", return_value={}), patch.object(handler, "send_json") as send, \
                patch("perfpilot.server.restart_adb_server") as restart:
            handler.do_POST()
        restart.assert_not_called()
        self.assertEqual(send.call_args.args[1], 403)

    def test_api_restarts_and_wakes_scanner(self):
        handler = Handler.__new__(Handler)
        handler.path = "/api/v1/adb/restart"
        handler.headers = {"Origin": "http://127.0.0.1:8765"}
        handler.server = SimpleNamespace(server_port=8765)
        with patch.object(handler, "read_json", return_value={}), patch.object(handler, "send_json") as send, \
                patch("perfpilot.server.manager.active", return_value=[]), \
                patch("perfpilot.server.restart_adb_server") as restart, \
                patch("perfpilot.server.registry.kick") as kick:
            handler.do_POST()
        restart.assert_called_once_with()
        kick.assert_called_once_with()
        self.assertEqual(send.call_args.args[0], {"status": "ok"})


if __name__ == "__main__":
    unittest.main()