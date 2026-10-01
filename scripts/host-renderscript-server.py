#!/usr/bin/env python3
"""Execute supported Android RenderScript operations on a host OpenCL GPU."""

import argparse
import socket
import struct
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compute"))
from intrinsics import ImageOps
from opencl import OpenCLError

MAX_MESSAGE = 64 * 1024 * 1024 + 24


def publish_port(path, number):
    temporary = path.with_suffix(".partial")
    temporary.write_text(str(number) + "\n")
    temporary.replace(path)


def read_exact(connection, size):
    data = bytearray(size)
    view = memoryview(data)
    offset = 0
    while offset < size:
        count = connection.recv_into(view[offset:])
        if not count:
            raise EOFError
        offset += count
    return data


def serve(connection, operations, lock):
    connection.settimeout(30)
    connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    with connection:
        try:
            while True:
                command, size = struct.unpack("<II", read_exact(connection, 8))
                if size > MAX_MESSAGE:
                    return
                payload = read_exact(connection, size)
                try:
                    if command == 0 and not size:
                        # Protocol version and supported intrinsic mask.
                        output = struct.pack("<II", 1, 1 << 5)
                    elif command == 5 and size >= 24:
                        width, height, channels, pitch, length, radius = struct.unpack_from("<5If", payload)
                        if length != size - 24:
                            raise ValueError("Truncated input")
                        with lock:
                            start = time.monotonic()
                            output = operations.blur(width, height, channels, pitch, radius, payload[24:])
                            print(f"RenderScript blur: {width}x{height}, {(time.monotonic() - start) * 1000:.1f} ms on GPU", flush=True)
                    else:
                        raise ValueError("Unsupported operation")
                    connection.sendall(struct.pack("<iI", 0, len(output)))
                    connection.sendall(output)
                except (OpenCLError, ValueError, OverflowError) as error:
                    print(f"RenderScript fallback: {error}", flush=True)
                    connection.sendall(struct.pack("<iI", -1, 0))
        except (EOFError, OSError):
            return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        operations = ImageOps()
    except (OpenCLError, OSError) as error:
        print(f"RenderScript uses the Android backend: {error}", flush=True)
        publish_port(args.port_file, 0)
        return
    lock = threading.Lock()
    print(f"RenderScript GPU: {operations.runtime.name} (OpenCL)", flush=True)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        publish_port(args.port_file, listener.getsockname()[1])
        try:
            while True:
                connection, _ = listener.accept()
                threading.Thread(target=serve, args=(connection, operations, lock), daemon=True).start()
        finally:
            operations.close()


if __name__ == "__main__":
    main()
