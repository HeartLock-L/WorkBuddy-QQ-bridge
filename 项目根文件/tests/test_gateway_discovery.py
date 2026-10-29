"""Exercise real gateway discovery using in-memory authentication responses."""

import asyncio
import hashlib
import json
import re
import types
import unittest
from unittest.mock import Mock

from bridge_test_support import load_definitions


class Response:
    """Represent one authentication response without opening a socket."""

    def __init__(self, status=200, body='{"authenticated": true}'):
        self.status = status
        self.body = body

    async def __aenter__(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self

    async def __aexit__(self, *_args):
        return None

    async def text(self):
        return self.body


class Session:
    """Record probes and return only explicitly configured fake responses."""

    def __init__(self):
        self.calls = []
        self.responses = {}

    def get(self, url, *, headers, timeout):
        match = re.fullmatch(r"http://127\.0\.0\.1:(\d+)/api/v1/auth/status", url)
        if match is None:
            raise AssertionError(f"Unexpected endpoint: {url}")
        port = int(match.group(1))
        token = headers["Authorization"].removeprefix("Bearer ")
        self.calls.append((port, token))
        return self.responses.get(
            (port, token), self.responses.get(port, Response(body=ConnectionError()))
        )


class GatewayDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    """Keep changed credentials usable while limiting full listener scans."""

    def setUp(self):
        self.now = 1000.0
        self.port = 12345
        self.token = "fake-original"
        self.session = Session()
        self.scan = Mock(return_value=[])
        self.logger = Mock()
        self.namespace = {
            "asyncio": asyncio,
            "hashlib": hashlib,
            "json": json,
            "re": re,
            "time": types.SimpleNamespace(
                time=lambda: self.now, monotonic=lambda: self.now
            ),
            "logger": self.logger,
            "_cfg": lambda: (self.port, self.token),
            "_local_ports": self.scan,
            "aiohttp": types.SimpleNamespace(ClientTimeout=lambda **kwargs: kwargs),
        }
        load_definitions(
            self.namespace,
            functions={"_resolve_base", "_auth_ok"},
            constants={"_GW_CACHE", "GW_RESCAN_COOLDOWN_SEC"},
        )
        self.resolve = self.namespace["_resolve_base"]

    async def discover(self):
        return await asyncio.wait_for(self.resolve(self.session, self.token), 1)

    async def test_configured_authenticated_port_needs_no_listener_scan(self):
        self.session.responses[self.port] = Response()
        self.assertEqual(await self.discover(), f"http://127.0.0.1:{self.port}")
        self.assertEqual(self.session.calls, [(self.port, self.token)])
        self.scan.assert_not_called()

    async def test_cached_authenticated_port_needs_no_listener_scan(self):
        self.port = None
        self.namespace["_GW_CACHE"]["base"] = "http://127.0.0.1:23456"
        self.session.responses[23456] = Response()
        self.assertEqual(await self.discover(), "http://127.0.0.1:23456")
        self.assertEqual(self.session.calls, [(23456, self.token)])
        self.scan.assert_not_called()

    async def test_new_configuration_is_tried_before_old_cached_address(self):
        self.namespace["_GW_CACHE"]["base"] = "http://127.0.0.1:23456"
        self.session.responses[self.port] = Response()
        self.session.responses[23456] = Response()
        self.assertEqual(await self.discover(), f"http://127.0.0.1:{self.port}")
        self.assertEqual(self.session.calls, [(self.port, self.token)])
        self.scan.assert_not_called()

    async def test_port_drift_scans_after_known_candidates_fail(self):
        self.scan.return_value = [self.port, 23456]
        self.session.responses[23456] = Response()
        self.assertEqual(await self.discover(), "http://127.0.0.1:23456")
        self.assertEqual(
            self.session.calls, [(self.port, self.token), (23456, self.token)]
        )
        self.scan.assert_called_once()

    async def test_scan_cooldown_still_retries_a_known_address(self):
        self.assertIsNone(await self.discover())
        self.now += 1
        self.session.responses[self.port] = Response()
        self.assertEqual(await self.discover(), f"http://127.0.0.1:{self.port}")
        self.scan.assert_called_once()
        self.assertEqual(self.session.calls, [(self.port, self.token)] * 2)

    async def test_unchanged_failure_does_not_repeat_full_scan_during_cooldown(self):
        self.assertIsNone(await self.discover())
        for _ in range(2):
            self.now += 1
            self.assertIsNone(await self.discover())
        self.scan.assert_called_once()
        self.assertEqual(self.session.calls, [(self.port, self.token)] * 3)

    async def test_changed_configured_port_bypasses_previous_scan_cooldown(self):
        self.assertIsNone(await self.discover())
        self.now += 1
        self.port = 23456
        self.scan.return_value = [34567]
        self.session.responses[34567] = Response()
        self.assertEqual(await self.discover(), "http://127.0.0.1:34567")
        self.assertEqual(self.scan.call_count, 2)
        self.assertEqual(
            self.session.calls[-2:], [(23456, self.token), (34567, self.token)]
        )

    async def test_changed_password_bypasses_cooldown_without_retaining_plaintext(self):
        original = self.token
        self.assertIsNone(await self.discover())
        self.now += 1
        self.token = "fake-replaced"
        self.scan.return_value = [23456]
        self.session.responses[(23456, self.token)] = Response()
        self.assertEqual(await self.discover(), "http://127.0.0.1:23456")
        self.assertEqual(self.scan.call_count, 2)
        self.assertEqual(
            self.session.calls[-2:], [(self.port, self.token), (23456, self.token)]
        )
        retained = repr(self.namespace["_GW_CACHE"]) + repr(self.logger.mock_calls)
        self.assertNotIn(original, retained)
        self.assertNotIn(self.token, retained)

    async def test_authentication_requires_both_http_success_and_boolean_true(self):
        for status, body in (
            (401, '{"authenticated": true}'),
            (200, '{"authenticated": false}'),
            (200, '{"authenticated": "true"}'),
            (200, "not-json"),
        ):
            with self.subTest(status=status, body=body):
                self.session.responses[self.port] = Response(status, body)
                self.assertIsNone(await self.discover())

    async def test_failed_scan_is_retried_after_cooldown_expires(self):
        self.assertIsNone(await self.discover())
        self.now += self.namespace["GW_RESCAN_COOLDOWN_SEC"] + 1
        self.scan.return_value = [23456]
        self.session.responses[23456] = Response()
        self.assertEqual(await self.discover(), "http://127.0.0.1:23456")
        self.assertEqual(self.scan.call_count, 2)


if __name__ == "__main__":
    unittest.main()
