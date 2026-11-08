#!/usr/bin/env python3
"""Serve public synthetic test patterns to one test VM, never user media."""
import argparse
import http.server
import ipaddress
from pathlib import Path
import re
import subprocess
import tempfile

HTML = b"""<!doctype html><meta charset=utf-8><title>ZMacRDS calibration</title>
<style>body{background:black;color:white;text-align:center;font:22px system-ui}
video{display:block;width:min(80vw,1280px);margin:0 auto}button{font:inherit}</style>
<button>Play calibration</button><video playsinline loop></video>
<script>
const video=document.querySelector('video');
video.src=location.pathname+'.mp4';
document.querySelector('button').onclick=async()=>{await video.play();await video.requestFullscreen()};
</script>"""

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", required=True, type=ipaddress.IPv4Address)
    parser.add_argument("--guest", required=True, type=ipaddress.IPv4Address)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--port", type=int, default=6085)
    args = parser.parse_args()
    if not all(ip.is_private and not ip.is_loopback for ip in (args.bind, args.guest)):
        parser.error("use the private VM interface and guest IP")
    with tempfile.TemporaryDirectory(prefix="zmacrds-calibration-") as folder:
        def make_clip(name, waveform):
            subprocess.run([
                args.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=black:s=1280x720:r=30:d=90",
                "-f", "lavfi", "-i", f"aevalsrc={waveform}:s=48000:d=90",
                "-vf", "drawbox=x=540:y=260:w=200:h=200:color=white:t=fill:enable='lt(mod(t,2),0.1)'",
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-movflags", "faststart", "-shortest", str(Path(folder) / f"{name}.mp4"),
            ], check=True)
            (Path(folder) / name).write_bytes(HTML)

        names = set()
        for hz in (440, 880):
            tone = f"0.2*sin(2*PI*{hz}*t)"
            make_clip(str(hz), f"if(lt(mod(t\\,2)\\,0.1)\\,{tone}\\,0)")
            make_clip(f"tone-{hz}", tone)
            names.update((str(hz), f"tone-{hz}"))
        allowed_paths = {f"/{name}{suffix}" for name in names for suffix in ("", ".mp4")}

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=folder, **kw)

            def do_GET(self):
                if self.client_address[0] != str(args.guest) or self.path not in allowed_paths:
                    self.send_error(403)
                    return
                if not self.path.endswith(".mp4"):
                    super().do_GET()
                    return
                data = (Path(folder) / self.path[1:]).read_bytes()
                start, end = 0, len(data) - 1
                selected = self.headers.get("Range")
                if selected:
                    match = re.fullmatch(r"bytes=(\d+)-(\d*)", selected)
                    if not match:
                        self.send_error(416)
                        return
                    start = int(match[1])
                    if match[2]:
                        end = min(end, int(match[2]))
                    if start > end:
                        self.send_error(416)
                        return
                self.send_response(206 if selected else 200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end-start+1))
                if selected:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                self.end_headers()
                self.wfile.write(data[start:end+1])

            def do_HEAD(self):
                self.send_error(405)

            def guess_type(self, path):
                return "video/mp4" if path.endswith(".mp4") else "text/html; charset=utf-8"

        with http.server.ThreadingHTTPServer((str(args.bind), args.port), Handler) as server:
            print(f"Calibration available to VM {args.guest} at http://{args.bind}:{args.port}/440, /880, /tone-440 and /tone-880", flush=True)
            server.serve_forever()

if __name__ == "__main__":
    main()
