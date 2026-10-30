"""Check that edits to split modules reach the production reload watcher."""

import asyncio
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from bridge_test_support import PLUGIN, load_definitions, load_module

SOURCES = load_module(PLUGIN.with_name("source_files.py"), "reload_sources_under_test")


class SourceReloadTests(unittest.IsolatedAsyncioTestCase):
    def reload_harness(self):
        """Load only the owned reload coordinator with an offline manager."""

        async def sleep(_delay):
            await asyncio.sleep(0)

        namespace = {
            "asyncio": types.SimpleNamespace(
                sleep=sleep,
                create_task=asyncio.create_task,
                wait=asyncio.wait,
            ),
            "logger": Mock(),
        }
        load_definitions(namespace, methods={"_spawn_reload"})
        bridge = namespace["Bridge"]()
        bridge._reload_task = None
        bridge._memory = types.SimpleNamespace(_io_tasks=set())
        bridge._purge_own_modules = Mock(return_value=4)
        bridge.context = types.SimpleNamespace(
            _star_manager=types.SimpleNamespace(
                reload=AsyncMock(return_value=(True, ""))
            )
        )
        return bridge, namespace

    async def test_reload_waits_for_actual_memory_completion_before_purging(self):
        bridge, _namespace = self.reload_harness()
        pending = asyncio.get_running_loop().create_future()
        bridge._memory._io_tasks.add(pending)
        seen = asyncio.Event()

        async def terminate():
            seen.set()
            if not pending.done():
                raise RuntimeError("Memory cleanup is still running")

        bridge.terminate = AsyncMock(side_effect=terminate)
        bridge._spawn_reload()
        coordinator = bridge._reload_task
        bridge._spawn_reload()
        self.assertIs(bridge._reload_task, coordinator)
        await asyncio.wait_for(seen.wait(), 1)
        await asyncio.sleep(0)
        bridge._purge_own_modules.assert_not_called()
        bridge.context._star_manager.reload.assert_not_awaited()
        self.assertFalse(pending.cancelled())
        pending.set_result(None)
        await asyncio.wait_for(coordinator, 1)
        self.assertEqual(bridge.terminate.await_count, 2)
        bridge._purge_own_modules.assert_called_once()
        bridge.context._star_manager.reload.assert_awaited_once_with("workbuddy_bridge")

    async def test_reload_rechecks_racing_completion_without_unbounded_retry(self):
        bridge, _namespace = self.reload_harness()
        bridge.terminate = AsyncMock(
            side_effect=[RuntimeError("last task finished"), None]
        )
        bridge._spawn_reload()
        await asyncio.wait_for(bridge._reload_task, 1)
        self.assertEqual(bridge.terminate.await_count, 2)
        bridge._purge_own_modules.assert_called_once()
        bridge.context._star_manager.reload.assert_awaited_once()

    async def test_reload_aborts_unrelated_cleanup_failure_before_purging(self):
        bridge, namespace = self.reload_harness()
        bridge.terminate = AsyncMock(side_effect=RuntimeError("unavailable fixture"))
        bridge._spawn_reload()
        await asyncio.wait_for(bridge._reload_task, 1)
        self.assertEqual(bridge.terminate.await_count, 2)
        bridge._purge_own_modules.assert_not_called()
        bridge.context._star_manager.reload.assert_not_awaited()
        namespace["logger"].warning.assert_called_once()

    async def test_each_split_module_triggers_reload_after_debounce(self):
        for name in SOURCES.SOURCE_NAMES[1:]:
            with self.subTest(module=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                entry = root / "main.py"
                for path in SOURCES.source_paths(entry):
                    path.write_text("VALUE = 1\n", encoding="utf-8")
                    os.utime(path, (1000, 1000))
                sleeps = []

                async def sleep(delay):
                    sleeps.append(delay)
                    if len(sleeps) == 1:
                        os.utime(root / name, (1010, 1010))
                    elif len(sleeps) > 2:
                        self.fail("The edited module was not detected by the watcher")

                namespace = {
                    "__file__": str(entry),
                    "Path": Path,
                    "source_files": SOURCES,
                    "asyncio": types.SimpleNamespace(sleep=sleep),
                    "HOT_RELOAD_INTERVAL": 2,
                    "HOT_RELOAD_DEBOUNCE": 1.5,
                    "logger": Mock(),
                }
                load_definitions(namespace, methods={"_hot_reload_loop"})
                bridge = namespace["Bridge"]()
                bridge._code_mtime = 1000
                bridge._spawn_reload = Mock()
                await asyncio.wait_for(bridge._hot_reload_loop(), 1)
                bridge._spawn_reload.assert_called_once_with()
                self.assertEqual(sleeps, [2, 1.5])
                self.assertEqual(bridge._code_mtime, 1010)


if __name__ == "__main__":
    unittest.main()
