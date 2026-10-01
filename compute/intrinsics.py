"""Host implementations of supported RenderScript intrinsics."""

import ctypes as c
import ctypes.util
import math
import struct
from functools import lru_cache
from pathlib import Path

from opencl import OpenCL, U


@lru_cache(maxsize=32)
def gaussian_weights(radius):
    """Use the Android 5 Gaussian definition and single-precision weights."""
    f32 = lambda value: c.c_float(value).value
    libm = c.CDLL(ctypes.util.find_library("m"))
    libm.powf.argtypes, libm.powf.restype = [c.c_float, c.c_float], c.c_float
    libm.sqrtf.argtypes, libm.sqrtf.restype = [c.c_float], c.c_float
    sigma = f32(f32(f32(0.4) * radius) + f32(0.6))
    coeff1 = f32(1 / f32(libm.sqrtf(f32(2 * f32(math.pi))) * sigma))
    coeff2 = f32(-1 / f32(f32(2 * sigma) * sigma))
    extent = math.ceil(radius)
    values, total = [], 0.0
    for position in range(-extent, extent + 1):
        value = f32(coeff1 * libm.powf(f32(math.e), f32(position * position * coeff2)))
        values.append(value)
        total = f32(total + value)
    scale = f32(1 / total)
    return extent, struct.pack("<" + "f" * len(values), *(f32(value * scale) for value in values))


class ImageOps:
    def __init__(self):
        self.runtime = OpenCL()
        self.program = self.runtime.program(Path(__file__).with_suffix(".cl").read_text())
        self.vertical = self.runtime.kernel(self.program, "blur_y")
        self.horizontal = self.runtime.kernel(self.program, "blur_x")

    def blur(self, width, height, channels, pitch, radius, data):
        if (channels not in (1, 4) or not 0 < width <= 16384 or not 0 < height <= 16384
                or not math.isfinite(radius) or not 0 < radius <= 25
                or pitch < width * channels
                or len(data) != pitch * (height - 1) + width * channels):
            raise ValueError("Unsupported blur allocation")
        output_size = width * height * channels
        if output_size > 32 * 1024 * 1024 or len(data) > 64 * 1024 * 1024:
            raise ValueError("Blur allocation is too large")
        extent, weights = gaussian_weights(radius)
        runtime, buffers = self.runtime, []
        try:
            for size, content in ((len(data), data), (output_size * 4, None),
                                  (len(weights), weights), (output_size, None)):
                buffers.append(runtime.buffer(size, content))
            source, temporary, coefficients, output = buffers
            runtime.run(self.vertical, [source, temporary, coefficients, U(width), U(height),
                                       U(channels), U(pitch), c.c_int(extent)], (width, height))
            runtime.run(self.horizontal, [temporary, output, coefficients, U(width), U(height),
                                         U(channels), c.c_int(extent)], (width, height))
            return runtime.read(output, output_size)
        finally:
            for buffer in buffers:
                runtime.lib.clReleaseMemObject(buffer)

    def close(self):
        self.runtime.lib.clReleaseKernel(self.vertical)
        self.runtime.lib.clReleaseKernel(self.horizontal)
        self.runtime.lib.clReleaseProgram(self.program)
        self.runtime.close()
