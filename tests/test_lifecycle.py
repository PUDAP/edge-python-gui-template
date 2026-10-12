"""Driver ownership and edge-exit cleanup without a real MCP subprocess."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

import gui_driver
import main


class DriverLifecycleTests(unittest.TestCase):
    def test_constructor_connects_and_close_uses_same_task(self):
        tasks = []

        async def record():
            tasks.append(asyncio.current_task())

        client = SimpleNamespace(start=mock.AsyncMock(side_effect=record),
                                 stop=mock.AsyncMock(side_effect=record))
        with mock.patch.object(gui_driver, "CuaDriverClient", return_value=client):
            d = gui_driver.GuiDriver("Demo")
        try:
            client.start.assert_awaited_once()
            client.stop.assert_not_awaited()
            self.assertTrue(d._loop_thread.is_alive())
        finally:
            d._close()
        d._close()
        client.stop.assert_awaited_once()
        self.assertIs(tasks[0], tasks[1])
        self.assertFalse(d._loop_thread.is_alive())
        self.assertTrue(d._loop.is_closed())
        with self.assertRaisesRegex(RuntimeError, "closed"):
            d._run(asyncio.sleep(0))

    def test_failed_initialization_stops_client_and_closes_loop(self):
        client = SimpleNamespace(start=mock.AsyncMock(side_effect=RuntimeError("connect failed")),
                                 stop=mock.AsyncMock())
        loop = asyncio.new_event_loop()
        with mock.patch.object(gui_driver, "CuaDriverClient", return_value=client), \
             mock.patch.object(gui_driver.asyncio, "new_event_loop", return_value=loop):
            with self.assertRaisesRegex(RuntimeError, "connect failed"):
                gui_driver.GuiDriver("Demo")
        client.stop.assert_awaited_once()
        self.assertTrue(loop.is_closed())

    def test_cleanup_error_still_closes_loop(self):
        client = SimpleNamespace(start=mock.AsyncMock(),
                                 stop=mock.AsyncMock(side_effect=RuntimeError("stop failed")))
        with mock.patch.object(gui_driver, "CuaDriverClient", return_value=client):
            d = gui_driver.GuiDriver("Demo")
        with self.assertLogs(gui_driver.logger, level="WARNING"):
            d._close()
        self.assertFalse(d._loop_thread.is_alive())
        self.assertTrue(d._loop.is_closed())


class MainCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_on_return_connect_failure_run_failure_and_cancellation(self):
        config = SimpleNamespace(machine_id="demo", target_app="Demo", nats_servers="nats://test",
                                 nats_server_list=["nats://test"])
        for stage in ("return", "connect", "run", "cancel"):
            with self.subTest(stage=stage):
                driver = mock.Mock()
                runner = SimpleNamespace(connect=mock.AsyncMock(), run=mock.AsyncMock())
                if stage == "connect":
                    runner.connect.side_effect = RuntimeError("connect failed")
                elif stage == "run":
                    runner.run.side_effect = RuntimeError("run failed")
                elif stage == "cancel":
                    runner.run.side_effect = asyncio.CancelledError()
                with mock.patch.object(main, "load_config", return_value=config), \
                     mock.patch.object(main, "Driver", return_value=driver), \
                     mock.patch.object(main, "EdgeNatsClient"), \
                     mock.patch.object(main, "EdgeRunner", return_value=runner):
                    if stage == "return":
                        await main.main()
                    else:
                        expected = asyncio.CancelledError if stage == "cancel" else RuntimeError
                        with self.assertRaises(expected):
                            await main.main()
                driver._close.assert_called_once_with()
