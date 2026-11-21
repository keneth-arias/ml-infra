import os
from pathlib import Path
import runpy
import select
import signal
import subprocess
import unittest
from unittest.mock import patch


STOP = runpy.run_path(str(
    Path(__file__).resolve().parents[1] / "bootstrap/nebius/management/stop-backend.py"
))["stop"]


class BackendStopTests(unittest.TestCase):
    def test_service_interpreter_supports_pidfds(self):
        subprocess.run([
            "/usr/bin/python3", "-I", "-c",
            "import os, signal; fd = os.pidfd_open(os.getpid()); "
            "signal.pidfd_send_signal(fd, 0); os.close(fd)",
        ], check=True)

    def test_no_supervisor_after_crash(self):
        with patch.dict(os.environ, {}, clear=True), patch("os.pidfd_open", create=True) as open_pid:
            STOP()
        open_pid.assert_not_called()

    def test_foreign_supervisor_is_never_signalled(self):
        with (
            patch.dict(os.environ, {"MAINPID": "91234"}),
            patch.object(Path, "read_text", side_effect=["0::/fixture\n", "93333\n"]),
            patch("os.pidfd_open", return_value=9, create=True),
            patch("os.close") as close,
            patch("signal.pidfd_send_signal", create=True) as send,
            patch("select.poll") as poll,
        ):
            poll.return_value.poll.return_value = []
            with self.assertRaisesRegex(ValueError, "outside"):
                STOP()
        send.assert_not_called()
        close.assert_called_once_with(9)

    def test_exited_supervisor_can_leave_a_draining_worker(self):
        with (
            patch.dict(os.environ, {"MAINPID": "91234"}),
            patch.object(Path, "read_text", side_effect=["0::/fixture\n", "91236\n", "91235\n91236\n", "91236\n"]),
            patch("os.getpid", return_value=91236),
            patch("os.pidfd_open", return_value=9, create=True),
            patch("os.close") as close,
            patch("signal.pidfd_send_signal", create=True) as send,
            patch("select.poll") as poll,
            patch("time.sleep") as wait,
        ):
            poll.return_value.poll.return_value = [(9, select.POLLIN)]
            STOP()
        poll.return_value.register.assert_called_once_with(9, select.POLLIN)
        poll.return_value.poll.assert_called_once_with(0)
        send.assert_not_called()
        close.assert_called_once_with(9)
        wait.assert_called_once()

    def test_waits_for_tracker_after_supervisor_exits(self):
        lists = ["0::/fixture\n", "91234\n91235\n", "91235\n91236\n", "91236\n"]
        with (
            patch.dict(os.environ, {"MAINPID": "91234"}),
            patch.object(Path, "read_text", side_effect=lists),
            patch("os.getpid", return_value=91236),
            patch("os.pidfd_open", return_value=9, create=True) as open_pid,
            patch("os.close"),
            patch("signal.pidfd_send_signal", create=True) as send,
            patch("time.sleep") as wait,
        ):
            STOP()
        open_pid.assert_called_once_with(91234)
        send.assert_called_once_with(9, signal.SIGTERM)
        wait.assert_called_once()


if __name__ == "__main__":
    unittest.main()
