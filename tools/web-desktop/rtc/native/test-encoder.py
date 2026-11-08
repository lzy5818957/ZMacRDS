"""Check the hardware encoder after its source changes from black to desktop."""
import pathlib
import struct
import subprocess
import sys
import tempfile


WIDTH, HEIGHT = 1920, 1200
FRAME_COUNT = 16
BLACK_FRAMES = 6
THUMBNAIL_SIZE = 8


def encode(encoder):
    black = bytes(WIDTH * HEIGHT * 4)
    red = bytes((20, 30, 230, 255))
    blue = bytes((220, 40, 20, 255))
    desktop = ((red * (WIDTH // 2)) + (blue * (WIDTH // 2))) * HEIGHT
    with tempfile.TemporaryDirectory() as temporary:
        work = pathlib.Path(temporary)
        with (work / "video").open("wb") as video:
            for index in range(FRAME_COUNT):
                video.write(struct.pack(">I", index))
                video.write(black if index < BLACK_FRAMES else desktop)
        (work / "audio").write_bytes(bytes(FRAME_COUNT * 3840))
        subprocess.run(
            ["/bin/sh", "-c", 'exec "$1" 3<video 4<audio 5>h264 6>opus', "test", encoder],
            cwd=work,
            check=True,
            timeout=15,
        )
        stream = bytearray()
        with (work / "h264").open("rb") as packets:
            for index in range(FRAME_COUNT):
                length, timestamp = struct.unpack(">II", packets.read(8))
                unit = packets.read(length)
                assert timestamp == index and len(unit) == length > 0
                stream.extend(unit)
            assert packets.read(1) == b""
        return stream


def verify(ffmpeg, stream):
    thumbnail = f"{THUMBNAIL_SIZE}:{THUMBNAIL_SIZE}"
    picture = subprocess.run(
        [ffmpeg, "-v", "error", "-f", "h264", "-i", "pipe:0", "-vf", f"scale={thumbnail}",
         "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"],
        input=stream,
        stdout=subprocess.PIPE,
        check=True,
        timeout=15,
    ).stdout
    frame_size = THUMBNAIL_SIZE ** 2 * 3
    assert len(picture) == FRAME_COUNT * frame_size
    for index in range(BLACK_FRAMES, FRAME_COUNT):
        frame = picture[index * frame_size:(index + 1) * frame_size]
        for row in range(1, THUMBNAIL_SIZE - 1):
            left = (row * THUMBNAIL_SIZE + 1) * 3
            right = (row * THUMBNAIL_SIZE + THUMBNAIL_SIZE - 2) * 3
            r, g, b = frame[left:left + 3]
            assert r > 175 and g < 85 and b < 85, (index, row, "red", r, g, b)
            r, g, b = frame[right:right + 3]
            assert b > 175 and r < 85 and g < 85, (index, row, "blue", r, g, b)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: test-encoder.py PATH_TO_ENCODER PATH_TO_FFMPEG")
    encoder, ffmpeg = (str(pathlib.Path(name).resolve()) for name in sys.argv[1:])
    verify(ffmpeg, encode(encoder))
    print("Native encoder preserved the desktop colors after the initial black frames.")
