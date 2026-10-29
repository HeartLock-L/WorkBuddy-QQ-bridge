"""Shared diagnostic fixtures with external operations blocked by default."""

import asyncio
import contextlib
import io
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge_test_support import ROOT, load_module

ORIGINAL_SOCKET_CONNECT = socket.socket.connect
sys.path.insert(0, str(ROOT / "scripts"))
from diagnostic_evidence import Evidence as Evidence
from diagnostic_evidence import inspect_runtime as inspect_runtime
from diagnostic_evidence import source_fingerprint as source_fingerprint

reload_tool = load_module(ROOT / "scripts/reload-plugin.py", "reload_plugin")
reconnect = load_module(ROOT / "scripts/reconnect.py", "reconnect")
doctor = load_module(ROOT / "scripts/doctor.py", "doctor")


class OfflineCase(unittest.TestCase):
    """Provide temporary state, captured output, and strict offline guards."""

    def run_async(self, coroutine):
        # Windows constructs its event-loop wakeup pipe using socketpair().
        # Permit only loop construction, then restore the external-network guard
        # before any tested coroutine executes.
        with patch.object(socket.socket, "connect", ORIGINAL_SOCKET_CONNECT):
            loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coroutine)
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Any accidentally unmocked external operation fails the test immediately.
        self.addCleanup(patch.stopall)
        patch.object(
            socket.socket, "connect", side_effect=AssertionError("network forbidden")
        ).start()
        patch.object(
            subprocess, "run", side_effect=AssertionError("external process forbidden")
        ).start()
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
