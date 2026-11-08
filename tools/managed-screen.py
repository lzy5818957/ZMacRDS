#!/usr/bin/env python3
"""Read and control an Apple Screen Sharing desktop on loopback only."""
import asyncio
import importlib.util
import json
import re
import struct
import sys
import zlib

import numpy as np

W, H = 1920, 1200
FIRST_FRAME_SECONDS = 45
NORMAL = [
    0xff1b, *map(ord, '1234567890-='), 0xff08, 0xff09,
    *map(ord, 'qwertyuiop[]'), 0xff0d, 0xffe3, *map(ord, "asdfghjkl;'\x60"),
    0xffe1, ord('\\'), *map(ord, 'zxcvbnm,./'), 0xffe2, ord('*'), 0xffe9, 32, 0xffe5,
    *range(0xffbe, 0xffc8),
]
EXT = {29: 0xffe4, 56: 0xffea, 71: 0xff50, 72: 0xff52, 73: 0xff55, 75: 0xff51,
       77: 0xff53, 79: 0xff57, 80: 0xff54, 81: 0xff56, 83: 0xffff, 91: 0xffeb, 92: 0xffec}


def gateway():
    spec = importlib.util.spec_from_file_location('zmacrds_native_vnc', '/usr/local/libexec/zmacrds-vnc-gateway.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def packet(data):
    sys.stdout.buffer.write(struct.pack('>BI', 1, len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


async def read_display_info(reader):
    width, height, count, flags = struct.unpack('>4H', await reader.readexactly(8))
    # On recent Apple servers the first count field is reserved and a
    # version-1 list puts its count after the flags (ten-byte header).
    if count == 0 and flags == 1:
        count = int.from_bytes(await reader.readexactly(2), 'big')
    if count > 8:
        raise ValueError('too many Apple displays')
    await reader.readexactly(28 * count)
    return width, height


async def virtual_init(reader, writer, asyncvnc):
    """Select the private Apple desktop explicitly. Never fall back to console."""
    async def block(minimum, maximum):
        size = int.from_bytes(await reader.readexactly(2), 'big')
        if not minimum <= size <= maximum:
            raise ValueError('invalid Apple session response')
        return await reader.readexactly(size)

    writer.write(b'\xc1')  # shared session + extended init + explicit selection
    await writer.drain()
    width, height, size = struct.unpack('>HH16xI', await reader.readexactly(24))
    if not 22 <= size <= 8192:
        raise ValueError('Apple session selection is unavailable')
    extension = await reader.readexactly(size)
    flags = int.from_bytes(extension[2:6], 'big')
    if extension[0] != 0 or not flags & 4:
        raise ValueError('Apple did not offer an isolated virtual desktop')
    if flags & 8:
        raise ValueError('macOS Screen Sharing cannot read the desktop')
    options = await block(10, 256)
    version, allowed = struct.unpack_from('>HI', options)
    if version != 1 or not allowed & (1 << 2):
        raise ValueError('virtual desktop choice unavailable')
    # Echo the console selector supplied by the server; command 2 selects
    # the virtual desktop of the authenticated user, never the console.
    console = options[10:].split(b'\0', 1)[0][:63]
    writer.write(struct.pack('>HHIBx64s', 72, 1, 0, 2, console))
    await writer.drain()
    while True:
        version, status = struct.unpack_from('>HI', await block(6, 256))
        if version != 1 or status not in (0, 2, 3, 4):
            raise ValueError('Apple denied the isolated desktop')
        if status in (0, 4):
            break

    commands = bytearray(32)
    commands[0], commands[2], commands[3], commands[4], commands[10] = 0xb0, 0x0c, 3, 0x90, 0x40
    writer.write(struct.pack('>BBHHI6I32s', 0x21, 0, 62, 1, 2, 6, 1, 0, 15, 0, 0, commands))
    writer.write(b'\x0a\x00\x00\x01\x0d\x01\0\0\0\0\0\0')
    writer.write(bytes(4) + struct.pack('>BBBBHHHBBB3x', 32, 24, 0, 1, 255, 255, 255, 16, 8, 0))
    writer.write(struct.pack('>BxH4i', 2, 4, 6, 0, 1101, -223))
    await writer.drain()
    # Apple usually reports zero-sized ServerInit while it creates the private
    # display. Wait for its display notice before requesting any framebuffer.
    if not width or not height:
        while True:
            kind = await reader.readexactly(1)
            if kind == b'\x14':
                data = await reader.readexactly(3)
                size = int.from_bytes(data[1:], 'big')
                if not 4 <= size <= 8192:
                    raise ValueError('invalid Apple status')
                state = await reader.readexactly(size)
                if int.from_bytes(state[2:4], 'big') in (1, 17):
                    raise ValueError('Apple desktop identity ended')
                continue
            if kind == b'\x07':
                continue
            if kind != b'\0':
                raise ValueError('missing Apple display information')
            header = await reader.readexactly(3)
            count = int.from_bytes(header[1:], 'big')
            if not 1 <= count <= 8:
                raise ValueError('invalid Apple display information')
            for _ in range(count):
                _, _, w, h, encoding = struct.unpack('>HHHHi', await reader.readexactly(12))
                if encoding == 1101:
                    w, h = await read_display_info(reader)
                elif encoding != -223:
                    raise ValueError('unexpected pixels before Apple display information')
                if w and h:
                    width, height = w, h
            if width and height:
                break
    return asyncvnc.Video(reader, writer, zlib.decompressobj().decompress, 'Private Mac desktop', width, height, 'bgra')


async def input_events(client, dims, visible, reader):
    width, height, xoff, yoff, vw, vh = dims
    held, buttons = set(), 0
    x = y = 0
    event = None
    def key(code, down):
        client.writer.write(struct.pack('>BBHI', 4, down, 0, code))
    def pointer(mask):
        client.writer.write(struct.pack('>BBHH', 5, mask, x, y))
    try:
        while raw := await reader.readline():
            if len(raw) > 4096 or not raw.endswith(b'\n'):
                raise ValueError('desktop input too large')
            if not visible.is_set():
                continue
            event = json.loads(raw)
            if not isinstance(event, dict):
                continue
            kind = event.get('type')
            if kind == 'key':
                number, down = event.get('code'), event.get('down')
                if type(number) is not int or type(down) is not bool:
                    continue
                if event.get('extended') is True:
                    symbol = EXT.get(number)
                elif 1 <= number <= len(NORMAL):
                    symbol = NORMAL[number-1]
                else:
                    symbol = {87: 0xffc8, 88: 0xffc9}.get(number)
                if symbol:
                    key(symbol, down)
                    (held.add if down else held.discard)(symbol)
            elif kind == 'text' and isinstance(event.get('text'), str) and len(event['text']) <= 64:
                for char in event['text']:
                    code = ord(char)
                    if code >= 32:
                        symbol = code if code < 256 else 0x1000000 | code
                        key(symbol, True)
                        key(symbol, False)
            elif kind in ('move', 'button', 'scroll'):
                px, py = event.get('x'), event.get('y')
                if type(px) is not int or type(py) is not int:
                    continue
                x = max(0, min(width - 1, (px - xoff) * width // vw))
                y = max(0, min(height - 1, (py - yoff) * height // vh))
                if kind == 'button' and type(event.get('button')) is int and event['button'] in (0, 1, 2):
                    bit = 1 << event['button']
                    buttons = buttons | bit if event.get('down') is True else buttons & ~bit
                if kind == 'scroll' and type(event.get('delta')) is int and event['delta']:
                    pointer(buttons | (8 if event['delta'] > 0 else 16))
                pointer(buttons)
            await client.drain()
    finally:
        if visible.is_set():
            for symbol in held:
                key(symbol, False)
            pointer(0)


async def frames(client, dims, visible, clear_login):
    width, height, xoff, yoff, vw, vh = dims
    canvas = np.zeros((H, W, 4), np.uint8)
    ysample = np.arange(vh) * height // vh
    xsample = np.arange(vw) * width // vw
    def refresh():
        client.writer.write(struct.pack('>BBHHHH', 3, 0, 0, 0, width, height))
        subscribe_updates(client.writer, width, height)
    # Recover both the picture and the push subscription. A full-frame pull
    # alone can make a stalled video appear alive at one frame per timeout.
    refresh()
    await client.drain()
    pending = None
    deadline = asyncio.get_running_loop().time() + FIRST_FRAME_SECONDS
    def check_first_frame():
        if not visible.is_set() and asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError('no visible frame from the isolated desktop')
    try:
        while True:
            check_first_frame()
            pending = asyncio.create_task(client.reader.readexactly(1))
            while not (await asyncio.wait({pending}, timeout=2))[0]:
                check_first_frame()
                refresh()
                await client.drain()
            kind = pending.result()
            async with asyncio.timeout_at(None if visible.is_set() else deadline):
                updated = await display_update(client, kind, canvas, ysample, xsample, dims)
            if updated:
                if visible.is_set() or bool(canvas[:, :, :3].any()):
                    await asyncio.to_thread(packet, memoryview(canvas).cast('B'))
                    if not visible.is_set():
                        clear_login()
                        visible.set()
    finally:
        if pending and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


def subscribe_updates(writer, width, height):
    writer.write(struct.pack('>BBHIHHHH', 9, 0, 1, 0, 0, 0, width, height))


async def display_update(client, kind, canvas, ysample, xsample, dims):
    width, height, xoff, yoff, vw, vh = dims
    if kind == b'\x14':
        body = await client.reader.readexactly(7)
        if body[:5] != b'\0\0\4\0\1' or body[5:] in (b'\0\x01', b'\0\x11'):
            raise ValueError('native session no longer belongs to the account')
        # A tickle can replace pixel pushes after the remote user locks or
        # after the server changes the active display. Re-arm that same display.
        if body[5:] in (b'\0\x04', b'\0\x06'):
            subscribe_updates(client.writer, width, height)
            await client.drain()
        return
    if kind in (b'\x02', b'\x07'):
        return
    if kind == b'\x03':
        header = await client.reader.readexactly(7)
        count = int.from_bytes(header[3:], 'big')
        if count > 65536:
            raise ValueError('unexpected clipboard size')
        await client.reader.readexactly(count)
        return
    if kind != b'\0':
        raise ValueError('unknown Apple Screen Sharing message')
    head = await client.reader.readexactly(3)
    count = int.from_bytes(head[1:], 'big')
    if count > 4096:
        raise ValueError('too many display rectangles')
    updated = False
    for _ in range(count):
        # Apple's display notices can arrive again after the initial grant.
        # A geometry change requires renegotiating the browser session.
        raw = await client.reader.readexactly(12)
        x, y, w, h, encoding = struct.unpack('>HHHHi', raw)
        if encoding == 1101:
            w, h = await read_display_info(client.reader)
        if encoding in (1101, -223):
            if w and h and (w, h) != (width, height):
                raise ValueError('Apple desktop geometry changed')
            continue
        if not w or not h or x+w > width or y+h > height:
            raise ValueError('invalid Apple display rectangle')
        size = w*h*4
        if encoding == 0:
            pixels = await client.reader.readexactly(size)
        elif encoding == 6:
            compressed = int.from_bytes(await client.reader.readexactly(4), 'big')
            if not 1 <= compressed <= size + 65536:
                raise ValueError('invalid compressed Apple rectangle')
            pixels = client.video.decompress(await client.reader.readexactly(compressed), size+1)
        else:
            raise ValueError('unexpected Apple display encoding')
        if len(pixels) != size:
            raise ValueError('truncated Apple display rectangle')
        rectangle = np.frombuffer(pixels, np.uint8).reshape(h,w,4)
        if (width, height) == (vw, vh):
            # The native server and encoder use BGRA. Paint only dirty
            # rectangles, without converting or copying the rest of the screen.
            canvas[yoff+y:yoff+y+h,xoff+x:xoff+x+w] = rectangle
        else:
            if client.video.data is None:
                client.video.data = np.zeros((height, width, 4), np.uint8)
            client.video.data[y:y+h,x:x+w] = rectangle
        updated = True
    if updated and (width, height) != (vw, vh):
        canvas[yoff:yoff+vh,xoff:xoff+vw] = client.video.data[ysample[:, None], xsample]
    return updated


async def desktop(login):
    module = gateway()
    try:
        for attempt in range(2):
            visible = asyncio.Event()
            try:
                await desktop_attempt(login, module, visible)
                return
            except (TimeoutError, asyncio.IncompleteReadError):
                # macOS may start the requested Aqua session but miss the
                # first framebuffer update. Reattach to that same account.
                # Do not retry denied identity, key pin, auth, or protocol errors.
                if visible.is_set() or attempt:
                    raise
                await asyncio.sleep(1)
    finally:
        login.clear()


async def desktop_attempt(login, module, visible):
    async with asyncio.timeout(45) as wait:
        async with module.pinned_connection(5900, login['username'], login['password'], login['screen_key_sha256'], virtual_init=virtual_init) as client:
            width, height = client.video.width, client.video.height
            if not 0 < width <= 4096 or not 0 < height <= 2160:
                raise ValueError('unsupported Apple Screen Sharing geometry')
            wait.reschedule(None)
            scale = min(W / width, H / height)
            vw, vh = max(1, int(width*scale)), max(1, int(height*scale))
            dims = width, height, (W-vw)//2, (H-vh)//2, vw, vh
            client.writer.write(b'\x0a\x00\x00\x01')
            await client.drain()
            reader = asyncio.StreamReader(limit=4096)
            transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
            pending = [asyncio.create_task(frames(client, dims, visible, login.clear)),
                       asyncio.create_task(input_events(client, dims, visible, reader))]
            try:
                done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for job in done:
                    job.result()
            finally:
                transport.close()
                for job in pending:
                    job.cancel()
                await asyncio.gather(*pending, return_exceptions=True)


def main():
    try:
        # Do not prefetch keyboard events into the buffered file object; the
        # asynchronous pipe reader takes ownership after the login line.
        line = bytearray()
        while len(line) < 1024 and (char := sys.stdin.buffer.raw.read(1)) and char != b'\n':
            line.extend(char)
        if char != b'\n':
            raise ValueError('invalid desktop credential framing')
        login = json.loads(line)
        if set(login) != {'username', 'password', 'screen_key_sha256'} or not all(
            isinstance(login[key], str) and re.fullmatch(pattern, login[key]) for key, pattern in
            {'username': r'zr[a-f0-9]{24}', 'password': r'A9-[A-Za-z0-9_-]{43}', 'screen_key_sha256': r'[a-f0-9]{64}'}.items()
        ):
            raise ValueError('invalid desktop credentials')
        asyncio.run(desktop(login))
    except (ValueError, OSError, TimeoutError, EOFError, TypeError, KeyError, zlib.error):
        # Do not log the identity, secret, desktop contents or Apple token.
        print('Managed Screen Sharing session unavailable', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
