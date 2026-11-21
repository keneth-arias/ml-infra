"""Shutdown hook for the attended single-process SkyPilot 0.14.3 API."""
import signal

from sky.server import plugins
from sky.server.requests import requests


def exit_after_drain(signum, frame):
    raise SystemExit(0)


class AttendedShutdown(plugins.BasePlugin):
    load_contexts = frozenset({plugins.PluginContext.MAIN})

    def install(self, extension_context):
        # Uvicorn restores this handler and replays TERM after its HTTP drain.
        # Exiting via Python lets SkyPilot run its dispatcher and queue cleanup.
        signal.signal(signal.SIGTERM, exit_after_drain)

    def shutdown(self):
        # Run the pinned release's SQLite cleanup on the supervisor thread.
        # CPython waits for the aiosqlite thread before running atexit.
        requests._cleanup()
