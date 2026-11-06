#!/usr/bin/env python3
"""Experimental loopback VNC account gateway, using the native Mac backend.

Account authentication is delegated to the upstream Apple server. The gateway
owns one controller per configured account; a valid new login replaces it.
Requires an encrypted SSH tunnel on both external network legs. Apple DH
authenticates credentials but does not encrypt the subsequent RFB stream.
"""
import argparse
import asyncio
import hashlib
import json
import os
import re
import stat
import struct
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}), flush=True)


FRAME_DELIVERY_SECONDS = 1.0


class DisplayDeliveryTimeout(Exception):
    pass


def auto_update(width, height, enabled):
    # Apple AutoFrameBufferUpdate v1, full fixed framebuffer. Off is all ones.
    return struct.pack(">BBHIHHHH", 9, 0, 1, 0 if enabled else 0xffffffff,
                       0, 0, width, height)


async def forward_client(reader, writer, width, height):
    """Bounded standard RFB framing; arm native push after client format setup.

    Once armed, incremental requests are fulfilled by pushes rather than
    queued again upstream. Explicit full repaints still reach the Mac.
    """
    armed = False
    permitted = {0, 1, 6, 16, -223, -224, -232, -239, -240}
    while kind := await reader.read(1):
        number = kind[0]
        if number == 0:
            body = await reader.readexactly(19)
            if body[3] != 32 or body[6] != 1:
                raise ValueError("32-bit true-colour client required")
        elif number == 2:
            body = await reader.readexactly(3)
            count = int.from_bytes(body[1:], "big")
            if count > 256:
                raise ValueError("oversized encoding list")
            raw = await reader.readexactly(count * 4)
            offered = [struct.unpack_from(">i", raw, at)[0] for at in range(0, len(raw), 4)]
            selected = [value for value in offered if value in permitted]
            if 0 not in selected:
                selected.append(0)
            body = b"\0" + len(selected).to_bytes(2, "big") + b"".join(
                struct.pack(">i", value) for value in selected)
        elif number == 3:
            body = await reader.readexactly(9)
            incremental, x, y, w, h = struct.unpack(">BHHHH", body)
            if incremental > 1 or not w or not h or x+w > width or y+h > height:
                raise ValueError("framebuffer request outside fixed geometry")
            full_view = (x, y, w, h) == (0, 0, width, height)
            if armed and full_view and incremental:
                continue
            if armed and not full_view:
                writer.write(auto_update(width, height, False))
                armed = False
            writer.write(kind + body)
            if full_view and not armed:
                writer.write(auto_update(width, height, True))
                armed = True
            await writer.drain()
            continue
        elif number == 4:
            body = await reader.readexactly(7)
        elif number == 5:
            body = await reader.readexactly(5)
        elif number == 6:
            body = await reader.readexactly(7)
            count = int.from_bytes(body[3:], "big", signed=True)
            if not 0 <= count <= 65536:
                raise ValueError("unsupported or oversized clipboard message")
            body += await reader.readexactly(count)
        else:
            raise ValueError("unsupported client message")
        writer.write(kind + body)
        await writer.drain()
    return "client_eof"


async def forward_display(reader, writer, width, height):
    """Stream opaque tiles with a deadline over each complete update.

    Native capture must never be held indefinitely by a slow downstream while
    its connection lock also prevents reading that user's keyboard events.
    No screen decoding or unbounded encoded-frame queue is introduced.
    """
    async def transfer(count):
        if not 0 <= count <= 64 * 1024 * 1024:
            raise ValueError("oversized display payload")
        while count:
            block = await reader.readexactly(min(count, 32768))
            writer.write(block)
            await writer.drain()
            count -= len(block)

    while kind := await reader.read(1):
        if kind == b"\0":
            try:
                async with asyncio.timeout(FRAME_DELIVERY_SECONDS):
                    header = await reader.readexactly(3)
                    writer.write(kind + header)
                    rectangles = int.from_bytes(header[1:], "big")
                    total = 0
                    for index in range(rectangles):
                        if index >= 4096:
                            raise ValueError("too many rectangles")
                        rectangle = await reader.readexactly(12)
                        x, y, w, h, encoding = struct.unpack(">HHHHi", rectangle)
                        if encoding == -224:  # LastRect
                            writer.write(rectangle)
                            break
                        if encoding == -223:  # DesktopSize
                            if (w, h) != (width, height):
                                raise ValueError("remote geometry changed; reconnect required")
                            count = 0
                        elif encoding in (-232,):
                            count = 0
                        elif encoding in (-239, -240):
                            if w > 512 or h > 512:
                                raise ValueError("oversized cursor")
                            mask = ((w + 7) // 8) * h
                            count = w*h*4 + mask if encoding == -239 else 6 + mask*2
                        else:
                            if not w or not h or x+w > width or y+h > height:
                                raise ValueError("display rectangle outside fixed geometry")
                            if encoding in (6, 16):
                                size = await reader.readexactly(4)
                                count = int.from_bytes(size, "big")
                                writer.write(rectangle + size)
                                total += count
                                if total > 64 * 1024 * 1024:
                                    raise ValueError("oversized complete display update")
                                await transfer(count)
                                continue
                            if encoding == 0:
                                count = w*h*4
                            elif encoding == 1:
                                count = 4
                            else:
                                raise ValueError("unnegotiated display encoding")
                        total += count
                        if total > 64 * 1024 * 1024:
                            raise ValueError("oversized complete display update")
                        writer.write(rectangle)
                        await transfer(count)
                    await writer.drain()
            except TimeoutError as error:
                raise DisplayDeliveryTimeout() from error
        elif kind == b"\2":  # Bell
            writer.write(kind)
            await writer.drain()
        elif kind == b"\3":  # Standard ServerCutText
            header = await reader.readexactly(7)
            count = int.from_bytes(header[3:], "big")
            if count > 65536:
                raise ValueError("oversized server clipboard")
            writer.write(kind + header)
            await transfer(count)
        elif kind == b"\x14":  # Apple's bounded MiscStatus
            body = await reader.readexactly(7)
            if body[:5] != b"\0\0\4\0\1" or body[5:] == b"\0\x11":
                raise ValueError("native session identity changed")
            # Standard clients do not understand Apple's status messages.
        else:
            raise ValueError("unsupported server message")
    return "upstream_eof"


@asynccontextmanager
async def pinned_connection(port, username, password, expected_sha256, shared=True, decoder="zlib"):
    """Apple RSA handshake: compare the public key BEFORE sending credentials.

    Native Apple servers require the key-request exchange even with a known
    key. asyncvnc's host_key argument skips that exchange, which is rejected
    by the VM's server. Keep framing compatible with asyncvnc 1.3.0.
    """
    if decoder == "zrle":
        import asyncvnc2 as asyncvnc
    elif decoder == "zlib":
        import asyncvnc
    else:
        raise ValueError("unsupported test decoder")
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.serialization import load_der_public_key
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        if await reader.readexactly(12) not in (b"RFB 003.008\n", b"RFB 003.889\n"):
            raise ValueError("RFB 3.8 required")
        writer.write(b"RFB 003.008\n")
        types = await reader.readexactly(int.from_bytes(await reader.readexactly(1), "big"))
        if 33 not in types:
            raise ValueError("Apple RSA account authentication required")
        writer.write(b"\x21\x00\x00\x00\x0a\x01\x00RSA1\x00\x00\x00\x00")
        await writer.drain()
        size = int.from_bytes(await reader.readexactly(4), "big")
        if not 7 <= size <= 8192:
            raise ValueError("invalid key packet size")
        packet = await reader.readexactly(size)
        length = int.from_bytes(packet[2:6], "big")
        if packet[:2] not in (b"\x01\x00", b"\x00\x01") or length != len(packet) - 7:
            raise ValueError("invalid key packet")
        der = packet[6:6 + length]
        if hashlib.sha256(der).hexdigest() != expected_sha256:
            raise ValueError("server key mismatch before authentication")
        key = load_der_public_key(der)
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size != 2048:
            raise ValueError("2048-bit RSA server key required")
        aes = os.urandom(16)
        encryptor = Cipher(algorithms.AES(aes), modes.ECB()).encryptor()
        clear = asyncvnc.pack_ard(username) + asyncvnc.pack_ard(password)
        encrypted = encryptor.update(clear) + encryptor.finalize()
        payload = b"\x01\x00RSA1\0\x01" + encrypted + b"\0\x01" + key.encrypt(aes, padding.PKCS1v15())
        writer.write(len(payload).to_bytes(4, "big") + payload)
        await writer.drain()
        await reader.readexactly(4)
        if await reader.readexactly(4) != bytes(4):
            raise PermissionError("account authentication denied")
        if not shared:
            # Video.create emits ClientInit itself. Override that single write.
            class InitWriter:
                def write(self, data):
                    writer.write(b"\0" if data == b"\1" else data)
                def __getattr__(self, name):
                    return getattr(writer, name)
            init_writer = InitWriter()
        else:
            init_writer = writer
        video = await asyncvnc.Video.create(reader, init_writer)
        yield asyncvnc.Client(reader=reader, writer=writer, host_key=key,
                              clipboard=asyncvnc.Clipboard(writer), keyboard=asyncvnc.Keyboard(writer),
                              mouse=asyncvnc.Mouse(writer), video=video)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass


def configuration(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o077:
            raise ValueError("private owned configuration required")
        raw = os.read(fd, 65537)
        if len(raw) > 65536:
            raise ValueError("oversized configuration")
        cfg = json.loads(raw)
    finally:
        os.close(fd)
    if set(cfg) != {"port", "upstream_port", "upstream_key_sha256", "upstream_key_file", "accounts", "seconds", "key_file"}:
        raise ValueError("invalid configuration keys")
    if not all(type(cfg[k]) is int and 1024 <= cfg[k] <= 65535 for k in ("port", "upstream_port")):
        raise ValueError("invalid loopback port")
    if cfg["port"] == cfg["upstream_port"] or not 1 <= cfg["seconds"] <= 7200:
        raise ValueError("recursive target or invalid lifetime")
    if not re.fullmatch(r"[0-9a-f]{64}", cfg["upstream_key_sha256"]):
        raise ValueError("upstream key pin required")
    accounts = cfg["accounts"]
    if not isinstance(accounts, dict) or not 1 <= len(accounts) <= 8:
        raise ValueError("experimental limit is 1..8 accounts")
    if any(not re.fullmatch(r"[a-z0-9_-]{1,63}", name) or type(uid) is not int or uid < 501
           for name, uid in accounts.items()) or len(set(accounts.values())) != len(accounts):
        raise ValueError("distinct ordinary account routes required")
    return cfg


@dataclass(eq=False)
class Viewer:
    ident: int
    downstream: object
    upstream: object = None
    uid: int = None
    revoked: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)

    def close(self):
        self.revoked.set()
        self.downstream.close()
        if self.upstream:
            self.upstream.writer.close()


class Controllers:
    def __init__(self):
        self.current = {}
        self.lock = asyncio.Lock()

    async def claim(self, viewer):
        # Called only after native upstream account authentication and pinning.
        async with self.lock:
            old = self.current.get(viewer.uid)
            self.current[viewer.uid] = viewer
            if old:
                old.close()
                emit("vnc_takeover", uid=viewer.uid, old=old.ident, new=viewer.ident)
        if old:
            await asyncio.wait_for(old.finished.wait(), 3)
        return not viewer.revoked.is_set() and self.current.get(viewer.uid) is viewer

    async def release(self, viewer):
        async with self.lock:
            if self.current.get(viewer.uid) is viewer:
                del self.current[viewer.uid]


def decode_credentials(clear):
    if len(clear) != 128:
        raise ValueError("invalid credential size")
    fields = []
    for part in (clear[:64], clear[64:]):
        if b"\0" not in part:
            raise ValueError("unterminated credential")
        fields.append(part.split(b"\0", 1)[0].decode("utf-8"))
    if not fields[0] or not fields[1]:
        raise ValueError("empty credential")
    return fields


class Gateway:
    def __init__(self, cfg, dh_parameters, rsa_key, upstream_key):
        self.cfg = cfg
        self.parameters = dh_parameters
        self.rsa_key = rsa_key
        self.upstream_key = upstream_key
        self.controllers = Controllers()
        self.viewers = set()
        self.serial = 0
        self.failures = []

    async def authenticate(self, reader, writer):
        from cryptography.hazmat.primitives.asymmetric import dh
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        writer.write(b"RFB 003.008\n")
        await writer.drain()
        version = await reader.readexactly(12)
        if not re.fullmatch(rb"RFB 003\.\d{3}\n", version) or int(version[8:11]) < 7:
            raise ValueError("RFB 3.7 or later required")
        writer.write(b"\x02\x21\x1e")  # Apple account RSA or DH; no password-only fallback.
        await writer.drain()
        selected = await reader.readexactly(1)
        if selected == b"\x21":
            return await self.authenticate_rsa(reader, writer)
        if selected != b"\x1e":
            raise ValueError("Apple account authentication required")
        private = self.parameters.generate_private_key()
        numbers = private.public_key().public_numbers()
        size = (numbers.parameter_numbers.p.bit_length() + 7) // 8
        writer.write(struct.pack(">HH", 2, size) + numbers.parameter_numbers.p.to_bytes(size, "big") +
                     numbers.y.to_bytes(size, "big"))
        await writer.drain()
        encrypted = await reader.readexactly(128)
        public = int.from_bytes(await reader.readexactly(size), "big")
        if not 2 <= public <= numbers.parameter_numbers.p - 2:
            raise ValueError("invalid peer public key")
        peer = dh.DHPublicNumbers(public, numbers.parameter_numbers).public_key()
        shared = private.exchange(peer)
        # Legacy Apple DH interoperability; protected by the existing SSH tunnel.
        key = hashlib.md5(shared, usedforsecurity=False).digest()
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        return decode_credentials(decryptor.update(encrypted) + decryptor.finalize())

    async def authenticate_rsa(self, reader, writer):
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        size = int.from_bytes(await reader.readexactly(4), "big")
        if size != 10 or await reader.readexactly(size) != b"\x01\x00RSA1\0\0\0\0":
            raise ValueError("invalid account key request")
        key = self.rsa_key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        packet = b"\x01\x00" + len(key).to_bytes(4, "big") + key + b"\0"
        writer.write(len(packet).to_bytes(4, "big") + packet)
        await writer.drain()
        size = int.from_bytes(await reader.readexactly(4), "big")
        if size != 138 + self.rsa_key.key_size // 8:
            raise ValueError("invalid account credential packet")
        packet = await reader.readexactly(size)
        if packet[:8] != b"\x01\x00RSA1\0\x01" or packet[136:138] != b"\0\x01":
            raise ValueError("invalid account credential header")
        key = self.rsa_key.decrypt(packet[138:], padding.PKCS1v15())
        if len(key) != 16:
            raise ValueError("invalid session key")
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        clear = decryptor.update(packet[8:136]) + decryptor.finalize()
        writer.write(bytes(4))
        await writer.drain()
        return decode_credentials(clear)

    async def upstream(self, username, password):
        # Encrypt credentials to the pre-provisioned, pinned native server key.
        # Do not learn/trust a replacement key after transmitting a password.
        context = pinned_connection(self.cfg["upstream_port"], username, password,
                                    self.cfg["upstream_key_sha256"])
        try:
            client = await context.__aenter__()
            if not 0 < client.video.width <= 4096 or not 0 < client.video.height <= 2160:
                raise ValueError("unsupported geometry")
            # Apple starts standard viewers in observe mode. Request control
            # using SetMode, then respect screensharingd's initial key guard
            # before exposing the ready connection to the frontend client.
            # https://github.com/bouk/macvnc#the-protocol (macOS 27.0.1)
            client.writer.write(b"\x0a\x00\x00\x01")
            await client.drain()
            await asyncio.sleep(2.2)
            return context, client
        except BaseException:
            await context.__aexit__(None, None, None)
            raise

    async def serve(self, reader, writer):
        if len(self.viewers) >= 16:
            writer.close()
            return
        self.serial += 1
        viewer = Viewer(self.serial, writer)
        self.viewers.add(viewer)
        context = None
        pipes = []
        security_result_sent = False
        reason = "setup_failure"
        started = time.monotonic()
        try:
            from async_timeout import timeout
            now = time.monotonic()
            self.failures = [t for t in self.failures if now - t < 60]
            if len(self.failures) >= 10:
                raise PermissionError("temporary login rate limit")
            async with timeout(90):
                username, password = await self.authenticate(reader, writer)
                if username not in self.cfg["accounts"]:
                    raise PermissionError("account unavailable")
                context, client = await self.upstream(username, password)
                password = None
                viewer.upstream = client
                viewer.uid = self.cfg["accounts"][username]
                writer.write(bytes(4))  # SecurityResult success, after upstream validation.
                await writer.drain()
                security_result_sent = True
                await reader.readexactly(1)  # ClientInit; gateway owns control semantics.
                if not await self.controllers.claim(viewer):
                    return
                import asyncvnc
                mode = next(raw for raw, name in asyncvnc.video_modes.items() if name == client.video.mode)
                name = f"ZMacRDS VNC - UID {viewer.uid}".encode()
                writer.write(struct.pack(">HH", client.video.width, client.video.height) + mode + bytes(3) +
                             struct.pack(">I", len(name)) + name)
                await writer.drain()
            emit("vnc_connected", connection=viewer.ident, uid=viewer.uid)

            pipes = [asyncio.create_task(forward_client(reader, client.writer, client.video.width, client.video.height)),
                     asyncio.create_task(forward_display(client.reader, writer, client.video.width, client.video.height)),
                     asyncio.create_task(viewer.revoked.wait())]
            done, _ = await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
            reason = "replaced" if viewer.revoked.is_set() else next(iter(done)).result()
        except DisplayDeliveryTimeout:
            reason = "display_delivery_timeout"
        except (PermissionError, ValueError):
            reason = "protocol_or_identity_rejected" if security_result_sent else "authentication_or_setup_denied"
            if not security_result_sent:
                self.failures.append(time.monotonic())
            # Never reflect credential contents or upstream errors to the client.
                try:
                    writer.write(struct.pack(">II", 1, 13) + b"Login denied.")
                    await writer.drain()
                except ConnectionError:
                    pass
        except (ConnectionError, asyncio.IncompleteReadError, TimeoutError, OSError):
            reason = "transport_or_timeout"
        except Exception:
            reason = "internal_failure"
        finally:
            viewer.close()
            for task in pipes:
                task.cancel()
            await asyncio.gather(*pipes, return_exceptions=True)
            if context:
                try:
                    await asyncio.wait_for(context.__aexit__(None, None, None), 2)
                except (ConnectionError, TimeoutError):
                    pass
            await self.controllers.release(viewer)
            self.viewers.discard(viewer)
            viewer.finished.set()
            emit("vnc_disconnected", connection=viewer.ident, uid=viewer.uid, reason=reason,
                 duration_ms=round((time.monotonic() - started) * 1000))


async def main(cfg):
    from cryptography.hazmat.primitives.asymmetric import dh, rsa
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PublicFormat, PrivateFormat, NoEncryption, load_pem_private_key, load_der_public_key)
    with open(cfg["upstream_key_file"], "rb") as source:
        native_der = source.read(16385)
    if len(native_der) > 16384 or hashlib.sha256(native_der).hexdigest() != cfg["upstream_key_sha256"]:
        raise ValueError("native server public key pin mismatch")
    upstream_key = load_der_public_key(native_der)
    if not isinstance(upstream_key, rsa.RSAPublicKey) or upstream_key.key_size < 2048:
        raise ValueError("native server RSA key required")
    try:
        fd = os.open(cfg["key_file"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        fd = os.open(cfg["key_file"], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    else:
        with os.fdopen(fd, "rb") as source:
            meta = os.fstat(source.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o077:
                raise ValueError("private owned server key required")
            raw = source.read(16385)
            if len(raw) > 16384:
                raise ValueError("invalid server key size")
            key = load_pem_private_key(raw, password=None)
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size != 2048:
        raise ValueError("2048-bit RSA account key required")
    # Generate once; subsequent starts reuse owned public parameters. A GUI
    # password prompt must not lose its connection while the user is typing.
    parameter_path = cfg["key_file"] + ".dh.pem"
    try:
        fd = os.open(parameter_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        from cryptography.hazmat.primitives.serialization import ParameterFormat
        parameters = await asyncio.to_thread(dh.generate_parameters, generator=2, key_size=2048)
        fd = os.open(parameter_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(parameters.parameter_bytes(Encoding.PEM, ParameterFormat.PKCS3))
    else:
        from cryptography.hazmat.primitives.serialization import load_pem_parameters
        with os.fdopen(fd, "rb") as source:
            meta = os.fstat(source.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o077:
                raise ValueError("private owned DH parameter cache required")
            raw = source.read(16385)
        if len(raw) > 16384:
            raise ValueError("invalid DH parameter cache size")
        parameters = load_pem_parameters(raw)
    numbers = parameters.parameter_numbers()
    if not isinstance(parameters, dh.DHParameters) or numbers.g != 2 or numbers.p.bit_length() != 2048:
        raise ValueError("2048-bit generator-2 DH parameters required")
    gateway = Gateway(cfg, parameters, key, upstream_key)
    server = await asyncio.start_server(gateway.serve, "127.0.0.1", cfg["port"], limit=65536)
    emit("vnc_gateway_ready", port=cfg["port"], upstream_port=cfg["upstream_port"],
         account_count=len(cfg["accounts"]), lifetime_seconds=cfg["seconds"], security="loopback+SSH")
    emit("vnc_gateway_key", sha256=hashlib.sha256(key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).hexdigest())
    async with server:
        try:
            await asyncio.sleep(cfg["seconds"])
        finally:
            for viewer in list(gateway.viewers):
                viewer.close()
            if gateway.viewers:
                await asyncio.wait_for(asyncio.gather(*(v.finished.wait() for v in gateway.viewers)), 5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    args = parser.parse_args()
    asyncio.run(main(configuration(args.config)))
