#!/usr/bin/env python3
"""Run the SDK GLES renderer and publish completed frames for QEMU."""

import argparse
import ctypes
import mmap
import os
import signal
import struct
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library-dir", type=Path, required=True)
    parser.add_argument("--frames", type=Path, required=True)
    parser.add_argument("--width", type=int, default=720)
    parser.add_argument("--height", type=int, default=1280)
    args = parser.parse_args()
    directory = args.library_dir.resolve()
    for key, name in (("ANDROID_EGL_LIB", "lib64EGL_translator.so"),
                      ("ANDROID_GLESv1_LIB", "lib64GLES_CM_translator.so"),
                      ("ANDROID_GLESv2_LIB", "lib64GLES_V2_translator.so")):
        os.environ[key] = str(directory / name)
    args.frames.parent.mkdir(parents=True, exist_ok=True)
    length = 4096 + args.width * args.height * 4
    file = args.frames.open("w+b")
    os.fchmod(file.fileno(), 0o600)
    file.truncate(length)
    frames = mmap.mmap(file.fileno(), length)
    frames[:8] = b"G925GPU1"
    struct.pack_into("<Q5I", frames, 8, 0, args.width, args.height, 1,
                     0x1908, 0x1401)
    pixel_address = ctypes.addressof(ctypes.c_char.from_buffer(frames, 4096))
    sequence = 0
    post_type = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_void_p)

    @post_type
    def post(_context, width, height, direction, format_, type_, pixels):
        nonlocal sequence
        if (width, height) != (args.width, args.height):
            return
        if format_ != 0x1908 or type_ != 0x1401:
            return
        struct.pack_into("<Q", frames, 8, sequence + 1)
        struct.pack_into("<5I", frames, 16, width, height,
                         direction & 0xffffffff, format_, type_)
        ctypes.memmove(pixel_address, pixels, width * height * 4)
        sequence += 2
        struct.pack_into("<Q", frames, 8, sequence)

    renderer = ctypes.CDLL(str(directory / "lib64OpenglRender.so"),
                           mode=ctypes.RTLD_GLOBAL)
    renderer.initLibrary.restype = ctypes.c_int
    renderer.setStreamMode.argtypes = [ctypes.c_int]
    renderer.initOpenGLRenderer.argtypes = [ctypes.c_int, ctypes.c_int,
        ctypes.c_bool, ctypes.c_char_p, ctypes.c_size_t]
    renderer.initOpenGLRenderer.restype = ctypes.c_int
    renderer.setPostCallback.argtypes = [post_type, ctypes.c_void_p]
    renderer.getHardwareStrings.argtypes = [ctypes.POINTER(ctypes.c_char_p)] * 3
    if not renderer.initLibrary():
        raise SystemExit("GLES renderer initialization failed")
    renderer.setStreamMode(1)  # TCP, used by the QEMU goldfish pipe adapter.
    address = ctypes.create_string_buffer(256)
    if not renderer.initOpenGLRenderer(args.width, args.height, False,
                                       address, len(address)):
        raise SystemExit("Could not start GLES renderer")
    renderer.setPostCallback(post, None)
    args.frames.with_suffix(".port").write_text(address.value.decode() + "\n")
    strings = [ctypes.c_char_p() for _ in range(3)]
    renderer.getHardwareStrings(*(ctypes.byref(value) for value in strings))
    print("Renderer:", *(value.value.decode(errors="replace")
                          if value.value else "unknown" for value in strings),
          flush=True)
    running = True

    def stop(_signal, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while running:
        time.sleep(0.2)
    renderer.stopOpenGLRenderer()
    frames.close()
    file.close()


if __name__ == "__main__":
    main()
