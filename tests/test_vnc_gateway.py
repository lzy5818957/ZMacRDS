import asyncio
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
import struct
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("vnc_gateway", ROOT / "tools/vnc_gateway.py")
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


class Writer:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class StreamWriter(Writer):
    def __init__(self):
        super().__init__()
        self.data = bytearray()

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass


class Framing(unittest.IsolatedAsyncioTestCase):
    async def source(self, data):
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        return reader

    async def test_full_request_arms_push_and_incremental_does_not_queue(self):
        full = struct.pack(">BBHHHH", 3, 0, 0, 0, 100, 80)
        incremental = struct.pack(">BBHHHH", 3, 1, 0, 0, 100, 80)
        key = struct.pack(">BBHI", 4, 1, 0, 120)
        writer = StreamWriter()
        result = await gateway.forward_client(await self.source(full+incremental+key), writer, 100, 80)
        self.assertEqual(result, "client_eof")
        self.assertEqual(writer.data, full+gateway.auto_update(100,80,True)+key)

    async def test_partial_view_disarms_without_changing_armed_geometry(self):
        full = struct.pack(">BBHHHH", 3, 0, 0, 0, 100, 80)
        partial = struct.pack(">BBHHHH", 3, 1, 10, 10, 20, 20)
        writer = StreamWriter()
        await gateway.forward_client(await self.source(full+partial), writer, 100, 80)
        self.assertEqual(writer.data, full+gateway.auto_update(100,80,True)+gateway.auto_update(100,80,False)+partial)

    async def test_only_parseable_encodings_are_negotiated(self):
        raw = b"\2\0\0\4" + struct.pack(">iiii",16,7,-239,12345)
        writer = StreamWriter()
        await gateway.forward_client(await self.source(raw), writer, 100, 80)
        self.assertEqual(writer.data,b"\2\0\0\3"+struct.pack(">iii",16,-239,0))

    async def test_compressed_pixels_and_cursor_are_streamed_without_reencoding(self):
        payload = b"opaque-codec-data"
        frame = b"\0\0\0\2"+struct.pack(">HHHHiI",0,0,100,80,16,len(payload))+payload
        frame += struct.pack(">HHHHi",1,1,2,2,-239)+bytes(18)
        writer = StreamWriter()
        await gateway.forward_display(await self.source(frame), writer, 100, 80)
        self.assertEqual(writer.data,frame)

    async def test_slow_downstream_expires_the_whole_update(self):
        writer = StreamWriter()
        writer.drain = AsyncMock(side_effect=lambda: None)
        async def slow():
            await asyncio.sleep(10)
        writer.drain = slow
        frame=b"\0\0\0\1"+struct.pack(">HHHHi",0,0,1,1,0)+bytes(4)
        with patch.object(gateway,"FRAME_DELIVERY_SECONDS",.01):
            with self.assertRaises(gateway.DisplayDeliveryTimeout):
                await gateway.forward_display(await self.source(frame),writer,100,80)

    async def test_session_change_and_geometry_change_fail_closed(self):
        for raw in [b"\x14\0\0\4\0\1\0\x11",
                    b"\0\0\0\1"+struct.pack(">HHHHi",0,0,200,80,-223)]:
            with self.assertRaises(ValueError):
                await gateway.forward_display(await self.source(raw),StreamWriter(),100,80)


class Controls(unittest.IsolatedAsyncioTestCase):
    async def test_native_control_mode_and_guard_precede_frontend_readiness(self):
        packets = []
        writer = SimpleNamespace(write=packets.append)
        client = SimpleNamespace(writer=writer, video=SimpleNamespace(width=1920, height=1200), drain=AsyncMock())
        @asynccontextmanager
        async def native(*args):
            yield client
        instance = gateway.Gateway(dict(upstream_port=5900, upstream_key_sha256="0"*64), None, None, None)
        with patch.object(gateway, "pinned_connection", native), patch.object(gateway.asyncio, "sleep", new_callable=AsyncMock) as guard:
            context, actual = await instance.upstream("test", "test-only")
            self.assertIs(actual, client)
            self.assertEqual(packets, [b"\x0a\0\0\1"])
            client.drain.assert_awaited_once()
            guard.assert_awaited_once_with(2.2)
            await context.__aexit__(None, None, None)

    @unittest.skipUnless(importlib.util.find_spec("cryptography"), "install tools/vnc-test-requirements.txt")
    async def test_changed_server_key_is_rejected_before_credentials(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        der = key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        received = []
        completed = asyncio.Event()

        async def server(reader, writer):
            try:
                writer.write(b"RFB 003.889\n")
                self.assertEqual(await reader.readexactly(12), b"RFB 003.008\n")
                writer.write(b"\1\x21")
                self.assertEqual(await reader.readexactly(15), b"\x21\0\0\0\x0a\1\0RSA1\0\0\0\0")
                packet = b"\0\1" + len(der).to_bytes(4, "big") + der + b"\0"
                writer.write(len(packet).to_bytes(4, "big") + packet)
                await writer.drain()
                received.append(await reader.read())
            finally:
                writer.close()
                completed.set()

        listener = await asyncio.start_server(server, "127.0.0.1", 0)
        try:
            with self.assertRaisesRegex(ValueError, "before authentication"):
                async with gateway.pinned_connection(listener.sockets[0].getsockname()[1],
                                                      "test", "never-send", "0" * 64):
                    self.fail("wrong server key accepted")
            await asyncio.wait_for(completed.wait(), 2)
            self.assertEqual(received, [b""])
        finally:
            listener.close()
            await listener.wait_closed()

    async def test_replacement_waits_for_old_shutdown_and_leaves_other_user(self):
        controls = gateway.Controllers()
        a = gateway.Viewer(1, Writer(), uid=505)
        b = gateway.Viewer(2, Writer(), uid=506)
        self.assertTrue(await controls.claim(a))
        self.assertTrue(await controls.claim(b))
        new = gateway.Viewer(3, Writer(), uid=505)
        pending = asyncio.create_task(controls.claim(new))
        await asyncio.sleep(0)
        self.assertTrue(a.revoked.is_set())
        self.assertFalse(b.revoked.is_set())
        self.assertFalse(pending.done())
        a.finished.set()
        self.assertTrue(await pending)
        await controls.release(a)
        self.assertIs(controls.current[505], new)
        self.assertIs(controls.current[506], b)

    async def test_latest_login_wins_even_while_previous_handoff_is_pending(self):
        controls = gateway.Controllers()
        old = gateway.Viewer(1, Writer(), uid=505)
        await controls.claim(old)
        middle = gateway.Viewer(2, Writer(), uid=505)
        one = asyncio.create_task(controls.claim(middle))
        await asyncio.sleep(0)
        latest = gateway.Viewer(3, Writer(), uid=505)
        two = asyncio.create_task(controls.claim(latest))
        await asyncio.sleep(0)
        old.finished.set()
        self.assertFalse(await one)
        middle.finished.set()
        self.assertTrue(await two)
        await controls.release(middle)
        self.assertIs(controls.current[505], latest)


class Credentials(unittest.TestCase):
    def test_padding_is_not_part_of_the_password(self):
        packet = b"rdp\0" + b"x" * 60 + b"secret\0" + b"y" * 57
        self.assertEqual(gateway.decode_credentials(packet), ["rdp", "secret"])

    def test_malformed_fields_are_not_valid_logins(self):
        for packet in [b"", b"x" * 128, bytes(128), b"\xff\0" + bytes(126)]:
            with self.assertRaises((ValueError, UnicodeError)):
                gateway.decode_credentials(packet)

    def test_private_config_rejects_public_and_symlinked_files(self):
        cfg = dict(port=5906, upstream_port=5903, upstream_key_sha256="1" * 64,
                   upstream_key_file="/private/native.der",accounts={"rdp": 505, "vnc": 506}, seconds=60, key_file="/private/test.pem")
        with tempfile.TemporaryDirectory() as directory:
            p = pathlib.Path(directory) / "config.json"
            p.write_text(json.dumps(cfg)); p.chmod(0o600)
            self.assertEqual(gateway.configuration(p)["accounts"], cfg["accounts"])
            p.chmod(0o644)
            with self.assertRaises(ValueError):
                gateway.configuration(p)
            p.chmod(0o600)
            link = pathlib.Path(directory) / "link.json"
            link.symlink_to(p)
            with self.assertRaises(OSError):
                gateway.configuration(link)
