"""Let the attended SkyPilot process tree leave its systemd unit in order."""
import os
from pathlib import Path
import select
import signal
import time


def stop():
    # systemd clears MAINPID when the supervisor has already exited.
    main = os.environ.get("MAINPID")
    if not main:
        return
    pid = int(main)
    if pid <= 1 or pid == os.getpid():
        raise ValueError("Invalid SkyPilot supervisor PID")
    group = next(line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines()
                 if line.startswith("0::/"))
    members = Path("/sys/fs/cgroup") / group.lstrip("/") / "cgroup.procs"
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        if main in members.read_text().splitlines():
            signal.pidfd_send_signal(fd, signal.SIGTERM)
        else:
            # The pidfd becomes readable at exit, including when the PID
            # disappears from the unit between pidfd_open and this read.
            exited = select.poll()
            exited.register(fd, select.POLLIN)
            if not any(events & (select.POLLIN | select.POLLHUP)
                       for _, events in exited.poll(0)):
                raise ValueError("SkyPilot supervisor is outside the stopping unit")
    except ProcessLookupError:
        pass
    finally:
        os.close(fd)

    # The multiprocessing resource tracker can finish just after the parent.
    # systemd enforces TimeoutStopSec and kills the cgroup if the drain stalls.
    own_pid = str(os.getpid())
    while set(members.read_text().splitlines()) - {own_pid}:
        time.sleep(0.1)


if __name__ == "__main__":
    stop()
