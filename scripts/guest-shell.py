#!/usr/bin/env python3
"""Run a bounded command through the emulator's local root UART console."""

import argparse
import re
import socket
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command")
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    token = uuid.uuid4().hex
    marker = "G925_DONE_" + token
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as console:
        console.settimeout(0.1)
        console.connect(str(ROOT / "working/g925-console.sock"))
        while True:
            try:
                if not console.recv(65536):
                    raise SystemExit("Guest console closed")
            except socket.timeout:
                break
        # Keep the completed marker out of the echoed input. A wrapped echo
        # line can otherwise look exactly like command completion.
        console.sendall(("\n" + args.command + "; G925_RESULT=$?; printf '\\nG925_DONE_%s:%s\\n' " +
                         token + " $G925_RESULT\n").encode())
        data = bytearray()
        deadline = time.monotonic() + args.timeout
        completed = False
        result = 0
        pattern = re.compile(rb"\r\n" + marker.encode() + rb":([0-9]+)\r\n")
        while time.monotonic() < deadline:
            try:
                chunk = console.recv(65536)
                if not chunk:
                    break
                data.extend(chunk)
                match = pattern.search(data)
                if match:
                    result = int(match.group(1))
                    completed = True
                    break
            except socket.timeout:
                continue
    output = data.decode(errors="replace").replace("\r", "")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
    else:
        print(output)
    if not completed:
        raise SystemExit("Guest command did not finish before timeout")
    if result:
        raise SystemExit(result)


if __name__ == "__main__":
    main()
