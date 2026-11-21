import os
from pathlib import Path
import runpy
import signal
import unittest
from unittest.mock import patch


STOP = runpy.run_path(str(
    Path(__file__).resolve().parents[1] / "bootstrap/nebius/management/stop-backend.py"
))["stop"]


class BackendStopTests(unittest.TestCase):
    def test_no_supervisor_after_crash(self):
        with patch.dict(os.environ, {}, clear=True), patch("os.pidfd_open") as open_pid:
            STOP()
        open_pid.assert_not_called()

    def test_foreign_supervisor_is_never_signalled(self):
        with (
            patch.dict(os.environ, {"MAINPID": "91234"}),
            patch.object(Path, "read_text", side_effect=["0::/fixture\n", "93333\n"]),
            patch("os.pidfd_open", return_value=9),
            patch("os.close") as close,
            patch("signal.pidfd_send_signal") as send,
        ):
            with self.assertRaisesRegex(ValueError, "outside"):
                STOP()
        send.assert_not_called()
        close.assert_called_once_with(9)

    def test_waits_for_tracker_after_supervisor_exits(self):
        lists = ["0::/fixture\n", "91234\n91235\n", "91235\n91236\n", "91236\n"]
        with (
            patch.dict(os.environ, {"MAINPID": "91234"}),
            patch.object(Path, "read_text", side_effect=lists),
            patch("os.getpid", return_value=91236),
            patch("os.pidfd_open", return_value=9) as open_pid,
            patch("os.close"),
            patch("signal.pidfd_send_signal") as send,
            patch("time.sleep") as wait,
        ):
            STOP()
        open_pid.assert_called_once_with(91234)
        send.assert_called_once_with(9, signal.SIGTERM)
        wait.assert_called_once()


if __name__ == "__main__":
    unittest.main()
