#!/usr/bin/env python3
"""Play paced 44.1 kHz stereo PCM with a bounded buffer on the Linux host."""

import argparse
import fcntl
import os
import select
import shutil
import signal
import socket
import struct
import subprocess
import time
from pathlib import Path

PCM_RATE = 44100 * 2 * 2
MAX_QUEUE = PCM_RATE // 10  # 100 ms; discard stale audio beyond this.
PREBUFFER = PCM_RATE * 60 // 1000  # Absorb TCG scheduling jitter.
MAGIC = 0x47393235


def backend_command():
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    if shutil.which("pw-cat") and (runtime / "pipewire-0").exists():
        return ["pw-cat", "--playback", "--raw", "--rate=44100", "--channels=2",
                "--format=s16", "--latency=80ms", "-"]
    if shutil.which("paplay"):
        return ["paplay", "--raw", "--rate=44100", "--channels=2",
                "--format=s16le", "--latency-msec=80"]
    if shutil.which("aplay"):
        return ["aplay", "-q", "-t", "raw", "-r", "44100", "-c", "2",
                "-f", "S16_LE", "--buffer-time=80000", "--period-time=20000"]
    raise SystemExit("No Linux audio client found; install pipewire-bin, pulseaudio-utils, or alsa-utils")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--port-file", type=Path, required=True)
    parser.add_argument("--vm-pid", type=int, help="stop when a live VM exits")
    args = parser.parse_args()
    backend = backend_command()
    running = True

    def stop(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    player = None
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16384)
        server.bind(("127.0.0.1", args.port))
        server.setblocking(False)
        args.port_file.write_text(str(server.getsockname()[1]) + "\n")
        print(f"Audio UDP listening: {server.getsockname()}, backend: {backend[0]}, queue cap: 100 ms", flush=True)
        queue = bytearray()
        ready_at = last_data = 0
        previous = sender = None
        try:
            while running:
                if args.vm_pid is not None:
                    try:
                        os.kill(args.vm_pid, 0)
                    except ProcessLookupError:
                        break
                if player is not None and player.poll() is not None:
                    raise RuntimeError(f"Audio backend exited with {player.returncode}")
                now = time.monotonic()
                writable = [player.stdin] if player and queue and now >= ready_at else []
                readable, writable, _ = select.select([server], writable, [], 0.01)
                if readable:
                    while True:
                        try:
                            data, address = server.recvfrom(2048)
                        except BlockingIOError:
                            break
                        if len(data) < 12 or (len(data) - 8) % 4:
                            continue
                        magic, sequence = struct.unpack_from("!II", data)
                        if magic != MAGIC:
                            continue
                        if address != sender:
                            # A restarted relay starts its sequence at zero.
                            sender, previous = address, None
                            queue.clear()
                            last_data = 0
                        if previous is not None:
                            difference = (sequence - previous) & 0xffffffff
                            if difference > 0x80000000:
                                continue
                        previous = sequence
                        pcm = data[8:]
                        now = time.monotonic()
                        if now - last_data > 0.25:
                            queue.clear()
                            ready_at = now + PREBUFFER / PCM_RATE
                        last_data = now
                        if player is None:
                            player = subprocess.Popen(backend, stdin=subprocess.PIPE, bufsize=0)
                            os.set_blocking(player.stdin.fileno(), False)
                            fcntl.fcntl(player.stdin, fcntl.F_SETPIPE_SZ, 4096)
                        queue.extend(pcm)
                        if len(queue) > MAX_QUEUE:
                            excess = (len(queue) - MAX_QUEUE + 3) // 4 * 4
                            del queue[:excess]
                if writable and queue:
                    try:
                        count = os.write(player.stdin.fileno(), queue[:4096])
                        del queue[:count]
                    except BlockingIOError:
                        pass
        finally:
            if player and player.poll() is None:
                player.terminate()
                try:
                    player.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    player.kill()
                    player.wait()
            args.port_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
