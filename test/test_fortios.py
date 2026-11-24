import asyncio
import errno
import io
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import asyncssh

from scrapli_scp.asyncscp.base import FileTransferResult
from scrapli_scp.asyncscp.fortinet_fortios import AsyncSCPFortiOS

MEMFD_CREATE = getattr(os, "memfd_create", None)


class GetConfigTests(unittest.IsolatedAsyncioTestCase):
    def new_driver(self):
        scp = AsyncSCPFortiOS(SimpleNamespace(
            host="firewall", timeout_ops=0,
            transport=SimpleNamespace(session=Mock()),
        ))
        scp._ensure_scp_capability = AsyncMock(return_value=True)
        scp._cleanup_after_transfer = AsyncMock()
        return scp

    def setUp(self):
        self.scp = self.new_driver()
        self.session = self.scp.conn.transport.session
        self.paths = []
        self.data = "config system global\r\n    set alias café\r\nend\r\n".encode()
        self.scp_mock = patch("scrapli_scp.asyncscp.base.asyncssh.scp", side_effect=self.download).start()
        self.memfd_patch = patch("scrapli_scp.asyncscp.fortinet_fortios.os.memfd_create", None, create=True)
        self.memfd_patch.start()
        self.addCleanup(patch.stopall)

    async def download(self, src, dst, **kwargs):
        self.paths.append(Path(dst))
        Path(dst).write_bytes(self.data)
        if kwargs.get("progress_handler"):
            kwargs["progress_handler"](src[1].encode(), os.fsencode(dst), len(self.data), len(self.data))

    def assert_temporary_files_removed(self):
        self.assertTrue(self.paths)
        for path in self.paths:
            self.assertFalse(path.exists())
            if path.parent != Path("/proc/self/fd"):
                self.assertFalse(path.parent.exists())

    def use_real_device_lifecycle(self):
        state = SimpleNamespace(enabled=False, events=[])

        async def send_command(command):
            if "admin-scp" in command:
                return SimpleNamespace(result="admin-scp : " + ("enable" if state.enabled else "disable"))
            if "get system status" in command:
                return SimpleNamespace(result="Version: FortiGate v7.4.4")
            return SimpleNamespace(result="admin ssh mgmt:1:22 root super_admin peer")

        async def send_commands(commands):
            if any("unset admin-scp" in command for command in commands):
                state.enabled = False
                state.events.append("disable")
            else:
                state.enabled = True
                state.events.append("enable")
            return SimpleNamespace(failed=False)

        del self.scp._ensure_scp_capability
        del self.scp._cleanup_after_transfer
        self.scp.conn.auth_username = "admin"
        self.scp.conn.context = AsyncMock()
        self.scp.conn.send_command = send_command
        self.scp.conn.send_commands = send_commands
        return state

    async def test_stringio_download_uses_asyncssh(self):
        stream = io.StringIO()
        progress = Mock()
        result = await self.scp.get_config(stream, progress_handler=progress)
        self.assertEqual(stream.getvalue(), self.data.decode())
        self.assertFalse(stream.closed)
        self.assertEqual(result, FileTransferResult(True, True, False))
        progress.assert_called_once_with(b"sys_config", os.fsencode(self.paths[0]), len(self.data), len(self.data))
        self.assertEqual(self.scp_mock.call_args.args[0], (self.session, "sys_config"))
        self.session.create_process.assert_not_called()
        self.scp._cleanup_after_transfer.assert_awaited_once()
        self.assert_temporary_files_removed()

    async def test_textiowrapper_writes_at_current_position(self):
        self.data = b"new"
        with io.TextIOWrapper(io.BytesIO(), encoding="utf-8") as stream:
            stream.write("prefix:")
            result = await self.scp.get_config(stream, sys_config=False)
            self.assertFalse(stream.closed)
            stream.seek(0)
            self.assertEqual(stream.read(), "prefix:new")
        self.assertTrue(result.transferred)
        self.assertEqual(self.scp_mock.call_args.args[0], (self.session, "fgt-config"))
        self.assert_temporary_files_removed()

    async def test_concurrent_downloads_have_separate_files(self):
        count = 12
        ready = asyncio.Event()

        async def download(src, dst, **kwargs):
            index = len(self.paths)
            self.paths.append(Path(dst))
            Path(dst).write_text(str(index), encoding="utf-8")
            if len(self.paths) == count:
                ready.set()
            await ready.wait()
            self.assertEqual(Path(dst).read_text(encoding="utf-8"), str(index))
            size = len(str(index))
            kwargs["progress_handler"](src[1].encode(), os.fsencode(dst), size, size)

        self.scp_mock.side_effect = download
        streams = [io.StringIO() for _ in range(count)]
        drivers = [self.new_driver() for _ in range(count)]
        results = await asyncio.wait_for(asyncio.gather(
            *(driver.get_config(stream) for driver, stream in zip(drivers, streams))
        ), timeout=5)
        self.assertTrue(all(result.transferred for result in results))
        self.assertEqual({stream.getvalue() for stream in streams}, {str(i) for i in range(count)})
        self.assertEqual(len(set(self.paths)), count)
        self.assert_temporary_files_removed()

    async def test_chunked_copy_runs_stream_io_off_event_loop(self):
        main_thread = threading.get_ident()
        write_threads = []

        class Stream(io.StringIO):
            def write(self, text):
                write_threads.append(threading.get_ident())
                return super().write(text)

        self.data = ("é" * 128001).encode()
        stream = Stream()
        await self.scp.get_config(stream)
        self.assertEqual(stream.getvalue(), self.data.decode())
        self.assertGreater(len(write_threads), 1)
        self.assertNotIn(main_thread, write_threads)
        self.assert_temporary_files_removed()

    async def test_empty_file(self):
        self.data = b""
        result = await self.scp.get_config(io.StringIO())
        self.assertTrue(result.transferred)
        self.assert_temporary_files_removed()

    async def test_failed_transfer_does_not_copy_partial_file(self):
        async def download(*args, **kwargs):
            await self.download(*args, **kwargs)
            raise asyncssh.SFTPFailure("Permission denied")

        self.scp_mock.side_effect = download
        stream = io.StringIO("original")
        result = await self.scp.get_config(stream)
        self.assertFalse(result.transferred)
        self.assertEqual(stream.getvalue(), "original")
        self.assertEqual(self.scp._cleanup_after_transfer.await_count, self.scp_mock.await_count)
        self.assert_temporary_files_removed()

    async def test_no_file_received(self):
        self.scp_mock.side_effect = None
        result = await self.scp.get_config(io.StringIO())
        self.assertFalse(result.transferred)

    async def test_decode_and_write_errors_remove_temporary_files(self):
        self.data = b"\xff"
        with self.assertRaises(UnicodeDecodeError):
            await self.scp.get_config(io.StringIO())
        self.assert_temporary_files_removed()
        self.data = b"config"
        stream = io.StringIO()
        stream.close()
        with self.assertRaises(ValueError):
            await self.scp.get_config(stream)
        self.assert_temporary_files_removed()

    async def test_cancellation_removes_partial_download(self):
        started = asyncio.Event()

        async def download(*args, **kwargs):
            await self.download(*args, **kwargs)
            started.set()
            await asyncio.Event().wait()

        self.scp_mock.side_effect = download
        stream = io.StringIO()
        task = asyncio.create_task(self.scp.get_config(stream))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(stream.getvalue(), "")
        self.assertEqual(self.scp_mock.await_count, 1)
        self.scp._cleanup_after_transfer.assert_awaited_once()
        self.assert_temporary_files_removed()

    async def test_memfd_creation_failure_uses_temporary_file(self):
        for error in (OSError(errno.ENOSYS, "Not supported"), PermissionError("Denied")):
            with self.subTest(error=error):
                with patch("scrapli_scp.asyncscp.fortinet_fortios.os.memfd_create",
                           side_effect=error, create=True) as create:
                    stream = io.StringIO()
                    result = await self.scp.get_config(stream)
                create.assert_called_once()
                self.assertTrue(result.transferred)
                self.assertEqual(stream.getvalue(), self.data.decode())
                self.assertNotEqual(self.paths[-1].parent, Path("/proc/self/fd"))
                self.assert_temporary_files_removed()

    async def test_channel_refusal_uses_fresh_connection(self):
        async def download(src, dst, **kwargs):
            if src[0] is self.session:
                raise asyncssh.ChannelOpenError(1, "refused")
            await self.download(src, dst, **kwargs)

        self.scp_mock.side_effect = download
        context = AsyncMock()
        fresh = context.__aenter__.return_value
        self.scp.conn.auth_username = "admin"
        self.scp.conn.auth_password = "password"
        with patch("scrapli_scp.asyncscp.base.connect", return_value=context):
            stream = io.StringIO()
            result = await self.scp.get_config(stream)
        self.assertTrue(result.transferred)
        self.assertEqual(self.scp_mock.call_args.args[0], (fresh, "sys_config"))
        self.assertEqual(stream.getvalue(), self.data.decode())
        self.assert_temporary_files_removed()

    async def test_existing_path_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "backup.conf"
            filename.write_text("original")
            result = await self.scp.get_config(str(filename))
            self.assertEqual(result, FileTransferResult(True, False, False))
            self.assertEqual(filename.read_text(), "original")
        self.scp_mock.assert_not_called()
        self.scp._cleanup_after_transfer.assert_awaited_once()

    async def test_path_download_still_uses_asyncssh_scp(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "backup.conf"
            result = await self.scp.get_config(str(filename))
            self.assertEqual(result, FileTransferResult(True, True, False))
            self.assertEqual(self.scp_mock.call_args.args[:2], ((self.session, "sys_config"), str(filename)))
            self.assertEqual(filename.read_bytes(), self.data)

    async def test_same_driver_serializes_device_setup_transfer_and_cleanup(self):
        state = self.use_real_device_lifecycle()
        active = 0
        maximum_active = 0

        async def download(src, dst, **kwargs):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            state.events.append("start")
            self.assertTrue(state.enabled)
            await asyncio.sleep(0)
            self.assertTrue(state.enabled)
            await self.download(src, dst, **kwargs)
            state.events.append("end")
            active -= 1

        self.scp_mock.side_effect = download
        streams = [io.StringIO() for _ in range(12)]
        results = await asyncio.wait_for(asyncio.gather(
            *(self.scp.get_config(stream, force_scp_config=True) for stream in streams)
        ), timeout=5)
        self.assertTrue(all(result.transferred for result in results))
        self.assertTrue(all(stream.getvalue() == self.data.decode() for stream in streams))
        self.assertEqual(maximum_active, 1)
        self.assertEqual(state.events, ["enable", "start", "end", "disable"] * 12)
        self.assertFalse(state.enabled)
        self.assert_temporary_files_removed()

    async def test_device_restored_after_cancelled_or_failed_transfer(self):
        state = self.use_real_device_lifecycle()
        started = asyncio.Event()

        async def download(*args, **kwargs):
            await self.download(*args, **kwargs)
            started.set()
            await asyncio.Event().wait()

        self.scp_mock.side_effect = download
        task = asyncio.create_task(self.scp.get_config(io.StringIO(), force_scp_config=True))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(state.enabled)

        self.scp_mock.side_effect = RuntimeError("Transfer failed")
        with self.assertRaisesRegex(RuntimeError, "Transfer failed"):
            await self.scp.get_config(io.StringIO(), force_scp_config=True)
        self.assertFalse(state.enabled)
        self.assertEqual(state.events, ["enable", "disable"] * 2)
        self.assert_temporary_files_removed()

    async def test_cleanup_false_and_preexisting_scp_setting_are_preserved(self):
        state = self.use_real_device_lifecycle()
        await self.scp.get_config(io.StringIO(), force_scp_config=True, cleanup=False)
        self.assertTrue(state.enabled)
        result = await self.scp.get_config(io.StringIO())
        self.assertTrue(result.transferred)
        self.assertTrue(state.enabled)
        self.assertNotIn("disable", state.events)
        self.assert_temporary_files_removed()

    async def test_cancellation_waits_for_open_write_and_flush_before_releasing_storage(self):
        real_open = open
        real_rmtree = shutil.rmtree
        loop = asyncio.get_running_loop()

        for stage in ("open", "write", "flush"):
            with self.subTest(stage=stage):
                started = asyncio.Event()
                release = threading.Event()
                readers = []

                def pause_worker():
                    loop.call_soon_threadsafe(started.set)
                    if not release.wait(5):
                        raise TimeoutError("Worker was not released")

                def open_source(*args, **kwargs):
                    source = real_open(*args, **kwargs)
                    readers.append(source)
                    if stage == "open":
                        pause_worker()
                    return source

                class Stream(io.StringIO):
                    def write(self, text):
                        if stage == "write":
                            pause_worker()
                        return super().write(text)

                    def flush(self):
                        if stage == "flush":
                            pause_worker()
                        return super().flush()

                def remove_directory(*args, **kwargs):
                    # Windows cannot delete a file while this reader is open.
                    if any(not source.closed for source in readers):
                        raise PermissionError("Source still open")
                    return real_rmtree(*args, **kwargs)

                stream = Stream()
                with patch("scrapli_scp.asyncscp.fortinet_fortios.open", side_effect=open_source, create=True), \
                     patch("shutil.rmtree", side_effect=remove_directory):
                    task = asyncio.create_task(self.scp.get_config(stream))
                    try:
                        await asyncio.wait_for(started.wait(), timeout=5)
                        task.cancel()
                        await asyncio.sleep(0)
                        task.cancel()
                        await asyncio.sleep(0)
                        self.assertFalse(task.done())
                        self.assertTrue(self.paths[-1].exists())
                    finally:
                        release.set()
                        outcome = await asyncio.gather(task, return_exceptions=True)
                self.assertIsInstance(outcome[0], asyncio.CancelledError)
                self.assertTrue(all(source.closed for source in readers))
                self.assertEqual(stream.getvalue(), self.data.decode())
                self.assertFalse(stream.closed)
                self.assert_temporary_files_removed()

    async def test_cancellation_waits_for_device_setup_then_restores_device(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def setup(**kwargs):
            started.set()
            await release.wait()
            return True

        self.scp._ensure_scp_capability.side_effect = setup
        task = asyncio.create_task(self.scp.get_config(io.StringIO()))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
        finally:
            release.set()
            outcome = await asyncio.gather(task, return_exceptions=True)
        self.assertIsInstance(outcome[0], asyncio.CancelledError)
        self.scp._cleanup_after_transfer.assert_awaited_once()
        self.scp_mock.assert_not_called()

    async def test_cancellation_waits_for_temporary_directory_creation_and_removal(self):
        real_directory = tempfile.TemporaryDirectory
        loop = asyncio.get_running_loop()
        for stage in ("create", "cleanup"):
            with self.subTest(stage=stage):
                started = asyncio.Event()
                release = threading.Event()
                directories = []

                def pause_worker():
                    loop.call_soon_threadsafe(started.set)
                    if not release.wait(5):
                        raise TimeoutError("Directory worker was not released")

                class Directory(real_directory):
                    def __init__(self, *args, **kwargs):
                        super().__init__(*args, **kwargs)
                        directories.append(Path(self.name))
                        if stage == "create":
                            pause_worker()

                    def cleanup(self):
                        if stage == "cleanup":
                            pause_worker()
                        super().cleanup()

                with patch("aiofiles.tempfile.syncTemporaryDirectory", Directory), \
                     patch("scrapli_scp.asyncscp.fortinet_fortios.os.memfd_create", None, create=True):
                    task = asyncio.create_task(self.scp.get_config(io.StringIO()))
                    try:
                        await asyncio.wait_for(started.wait(), timeout=5)
                        for _ in range(2):
                            task.cancel()
                            await asyncio.sleep(0)
                            self.assertFalse(task.done())
                        self.assertTrue(directories[0].exists())
                    finally:
                        release.set()
                        outcome = await asyncio.gather(task, return_exceptions=True)
                self.assertIsInstance(outcome[0], asyncio.CancelledError)
                self.assertTrue(all(not directory.exists() for directory in directories))

    async def test_repeated_cancellation_waits_for_device_cleanup(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def cleanup():
            started.set()
            await release.wait()

        self.scp._cleanup_after_transfer.side_effect = cleanup
        task = asyncio.create_task(self.scp.get_config(io.StringIO()))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
        finally:
            release.set()
            outcome = await asyncio.gather(task, return_exceptions=True)
        self.assertIsInstance(outcome[0], asyncio.CancelledError)
        self.scp._cleanup_after_transfer.assert_awaited_once()
        self.assert_temporary_files_removed()
        self.assertTrue((await self.scp.get_config(io.StringIO())).transferred)

    async def test_repeated_cancellation_waits_for_transfer_to_close(self):
        started = asyncio.Event()
        closing = asyncio.Event()
        release = asyncio.Event()

        async def download(*args, **kwargs):
            await self.download(*args, **kwargs)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closing.set()
                await release.wait()

        self.scp_mock.side_effect = download
        task = asyncio.create_task(self.scp.get_config(io.StringIO()))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            task.cancel()
            await asyncio.wait_for(closing.wait(), timeout=5)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertTrue(self.paths[-1].exists())
            self.scp._cleanup_after_transfer.assert_not_called()
        finally:
            release.set()
            outcome = await asyncio.gather(task, return_exceptions=True)
        self.assertIsInstance(outcome[0], asyncio.CancelledError)
        self.scp._cleanup_after_transfer.assert_awaited_once()
        self.assert_temporary_files_removed()


@unittest.skipUnless(MEMFD_CREATE is not None, "Linux memfd_create required")
class LinuxMemoryGetConfigTests(GetConfigTests):
    def setUp(self):
        super().setUp()
        self.memfd_patch.stop()

    async def test_memory_download_does_not_create_temporary_directory(self):
        with patch("scrapli_scp.asyncscp.fortinet_fortios.TemporaryDirectory") as temporary_directory:
            stream = io.StringIO()
            result = await self.scp.get_config(stream)
        temporary_directory.assert_not_called()
        self.assertEqual(self.paths[0].parent, Path("/proc/self/fd"))
        self.assertEqual(stream.getvalue(), self.data.decode())
        self.assertTrue(result.transferred)
        self.assert_temporary_files_removed()

    async def test_memory_download_failure_retries_using_temporary_file(self):
        for error in (OSError(errno.ENOSPC, "Full"), FileNotFoundError("No /proc"),
                      asyncssh.SFTPFailure("Write failed")):
            with self.subTest(error=error):
                self.paths.clear()

                async def download(src, dst, **kwargs):
                    if Path(dst).parent == Path("/proc/self/fd"):
                        self.paths.append(Path(dst))
                        Path(dst).write_bytes(b"partial")
                        raise error
                    await self.download(src, dst, **kwargs)

                self.scp_mock.side_effect = download
                stream = io.StringIO()
                result = await self.scp.get_config(stream)
                self.assertTrue(result.transferred)
                self.assertEqual(stream.getvalue(), self.data.decode())
                self.assertEqual(len(self.paths), 2)
                self.assertEqual(self.paths[0].parent, Path("/proc/self/fd"))
                self.assertNotEqual(self.paths[1].parent, Path("/proc/self/fd"))
                self.assert_temporary_files_removed()

    async def test_temporary_file_failure_after_memory_failure_propagates(self):
        async def download(src, dst, **kwargs):
            self.paths.append(Path(dst))
            raise PermissionError("Write denied")

        self.scp_mock.side_effect = download
        stream = io.StringIO()
        with self.assertRaises(PermissionError):
            await self.scp.get_config(stream)
        self.assertEqual(len(self.paths), 2)
        self.assertEqual(stream.getvalue(), "")
        self.assert_temporary_files_removed()

    async def test_output_stream_write_failure_does_not_retry(self):
        class Stream(io.StringIO):
            def write(self, text):
                raise OSError("Output stream failed")

        with self.assertRaisesRegex(OSError, "Output stream failed"):
            await self.scp.get_config(Stream())
        self.assertEqual(self.scp_mock.await_count, 1)
        self.assert_temporary_files_removed()


if __name__ == "__main__":
    unittest.main()
