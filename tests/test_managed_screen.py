import asyncio
import importlib.util
import pathlib
import struct
import unittest
from types import SimpleNamespace

path = pathlib.Path(__file__).resolve().parents[1]/'tools/managed-screen.py'
spec = importlib.util.spec_from_file_location('managed_screen', path)
screen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(screen)


class VirtualDesktopTests(unittest.IsolatedAsyncioTestCase):
    async def select(self, allowed, status=0, flags=0x56):
        def block(data):
            return struct.pack('>H', len(data))+data
        writer = SimpleNamespace(data=bytearray())
        writer.write = writer.data.extend
        async def drain():
            pass
        writer.drain = drain
        reader = asyncio.StreamReader()
        name = b'\0\0'+struct.pack('>I',flags)+b'\0'*16
        reader.feed_data(struct.pack('>HH16xI',1920,1200,len(name))+name+
                         block(struct.pack('>HII',1,allowed,0)+b'console\0')+
                         block(struct.pack('>HI',1,status)))
        reader.feed_eof()
        video = SimpleNamespace(Video=lambda *fields: fields)
        return writer, reader, video

    async def test_selects_only_the_virtual_desktop(self):
        writer, reader, video = await self.select(7)
        ready = await screen.virtual_init(reader, writer, video)
        self.assertEqual(ready[4:6], (1920,1200))
        self.assertEqual(writer.data[0], 0xc1)
        size, version, reserved, command = struct.unpack_from('>HHIB', writer.data, 1)
        self.assertEqual((size, version, reserved, command), (72,1,0,2))

    async def test_refuses_console_only_offer_and_denied_virtual_display(self):
        writer, reader, video = await self.select(3)
        with self.assertRaises(ValueError):
            await screen.virtual_init(reader, writer, video)
        self.assertEqual(writer.data, b'\xc1')
        writer, reader, video = await self.select(7, 1)
        with self.assertRaises(ValueError):
            await screen.virtual_init(reader, writer, video)
        self.assertEqual(len(writer.data), 75)

    async def test_screen_read_denied_before_virtual_selection(self):
        writer, reader, video = await self.select(7, flags=0x5f)
        with self.assertRaisesRegex(ValueError, 'cannot read the desktop'):
            await screen.virtual_init(reader, writer, video)
        self.assertEqual(writer.data, b'\xc1')

    async def test_display_info_stays_aligned_with_the_next_packet(self):
        # Display list captured from VM03, followed by the next RFB header.
        body = bytes.fromhex('078004b000000001000100000004078004b0000000002020000100ff00ff00ff100800000000')
        for payload in (body, struct.pack('>4H', 1920, 1200, 1, 0) + body[10:]):
            r = asyncio.StreamReader()
            r.feed_data(payload + b'\0\0\0\1')
            r.feed_eof()
            self.assertEqual(await screen.read_display_info(r), (1920, 1200))
            self.assertEqual(await r.readexactly(4), b'\0\0\0\1')

    async def test_black_desktop_cannot_receive_keyboard_input(self):
        data = bytearray()
        async def drain():
            pass
        client = SimpleNamespace(writer=SimpleNamespace(write=data.extend), drain=drain)
        visible = asyncio.Event()
        dims = (1920, 1200, 0, 0, 1920, 1200)
        key = b'{"type":"key","code":30,"down":true}\n'
        def stream():
            r = asyncio.StreamReader()
            r.feed_data(key)
            r.feed_eof()
            return r
        await screen.input_events(client, dims, visible, stream())
        self.assertEqual(data, b'')
        visible.set()
        await screen.input_events(client, dims, visible, stream())
        self.assertEqual(data[:8], struct.pack('>BBHI', 4, 1, 0, ord('a')))

    async def test_native_bgra_updates_only_dirty_rectangle(self):
        import numpy as np
        canvas = np.zeros((3, 4, 4), dtype=np.uint8)
        reader = asyncio.StreamReader()
        reader.feed_data(b'\0\0\1'+struct.pack('>HHHHi', 2, 1, 1, 1, 0)+bytes([27, 61, 198, 0]))
        client = SimpleNamespace(reader=reader)
        self.assertTrue(await screen.display_update(client, b'\0', canvas, None, None, (4,3,0,0,4,3)))
        self.assertEqual(canvas[1,2].tolist(), [27,61,198,0])
        self.assertEqual(int(np.count_nonzero(canvas)), 3)

    async def test_retry_only_before_first_frame(self):
        from unittest.mock import AsyncMock, patch
        calls = []
        async def attempt(login, module, visible):
            calls.append(dict(login))
            if len(calls) == 1:
                raise TimeoutError('first visible frame')
            visible.set()
        with patch.object(screen, 'gateway', return_value=object()), patch.object(screen, 'desktop_attempt', side_effect=attempt), patch.object(screen.asyncio, 'sleep', new=AsyncMock()):
            login={'username':'fixture'}
            await screen.desktop(login)
            self.assertEqual(calls, [{'username':'fixture'}]*2)
            self.assertEqual(login, {})
        with patch.object(screen, 'gateway', return_value=object()), patch.object(screen, 'desktop_attempt', side_effect=ValueError('server key mismatch')) as denied:
            with self.assertRaises(ValueError):
                await screen.desktop({'username':'fixture'})
            self.assertEqual(denied.await_count, 1)

    async def test_tickle_renews_push_for_same_display(self):
        data = bytearray()
        async def drain():
            pass
        reader = asyncio.StreamReader()
        reader.feed_data(b'\0\0\4\0\1\0\4')
        client = SimpleNamespace(reader=reader, writer=SimpleNamespace(write=data.extend), drain=drain)
        await screen.display_update(client, b'\x14', None, None, None, (1920,1200,0,0,1920,1200))
        self.assertEqual(data, struct.pack('>BBHIHHHH', 9, 0, 1, 0, 0, 0, 1920, 1200))
