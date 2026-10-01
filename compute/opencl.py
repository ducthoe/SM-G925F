"""Small OpenCL 1.2 GPU runtime; no vendor-specific APIs or Python packages."""

import ctypes as c
import ctypes.util


P = c.c_void_p
U = c.c_uint
Z = c.c_size_t
B = c.c_ulonglong
I = c.c_int


class OpenCLError(RuntimeError):
    pass


class OpenCL:
    def __init__(self):
        library = ctypes.util.find_library("OpenCL")
        if not library:
            raise OpenCLError("No OpenCL runtime")
        self.lib = c.CDLL(library)
        signatures = {
            "clGetPlatformIDs": (I, [U, c.POINTER(P), c.POINTER(U)]),
            "clGetDeviceIDs": (I, [P, B, U, c.POINTER(P), c.POINTER(U)]),
            "clGetDeviceInfo": (I, [P, U, Z, P, c.POINTER(Z)]),
            "clCreateContext": (P, [P, U, c.POINTER(P), P, P, c.POINTER(I)]),
            "clCreateCommandQueue": (P, [P, P, B, c.POINTER(I)]),
            "clCreateProgramWithSource": (P, [P, U, c.POINTER(c.c_char_p), c.POINTER(Z), c.POINTER(I)]),
            "clBuildProgram": (I, [P, U, c.POINTER(P), c.c_char_p, P, P]),
            "clGetProgramBuildInfo": (I, [P, P, U, Z, P, c.POINTER(Z)]),
            "clCreateKernel": (P, [P, c.c_char_p, c.POINTER(I)]),
            "clCreateBuffer": (P, [P, B, Z, P, c.POINTER(I)]),
            "clSetKernelArg": (I, [P, U, Z, P]),
            "clEnqueueNDRangeKernel": (I, [P, P, U, c.POINTER(Z), c.POINTER(Z), c.POINTER(Z), U, P, P]),
            "clEnqueueReadBuffer": (I, [P, P, U, Z, Z, P, U, P, P]),
            "clFinish": (I, [P]),
        }
        for name, (result, arguments) in signatures.items():
            function = getattr(self.lib, name)
            function.restype, function.argtypes = result, arguments
        for name in ("clReleaseMemObject", "clReleaseKernel", "clReleaseProgram",
                     "clReleaseCommandQueue", "clReleaseContext"):
            function = getattr(self.lib, name)
            function.restype, function.argtypes = I, [P]
        count = U()
        self.check(self.lib.clGetPlatformIDs(0, None, c.byref(count)), "platforms")
        platforms = (P * count.value)()
        self.check(self.lib.clGetPlatformIDs(count.value, platforms, None), "platforms")
        candidates = []
        for platform in platforms:
            count = U()
            if self.lib.clGetDeviceIDs(platform, 4, 0, None, c.byref(count)):
                continue
            devices = (P * count.value)()
            self.check(self.lib.clGetDeviceIDs(platform, 4, count.value, devices, None), "GPU devices")
            for device in devices:
                if not self.info_number(device, 0x1027, U):  # CL_DEVICE_AVAILABLE
                    continue
                unified = self.info_number(device, 0x1035, U)
                units = self.info_number(device, 0x1002, U)
                memory = self.info_number(device, 0x101F, B)
                candidates.append(((not unified, units, memory), device))
        if not candidates:
            raise OpenCLError("No available OpenCL GPU")
        self.device = P(max(candidates, key=lambda item: item[0])[1])
        self.name = self.info_string(self.device, 0x102B)
        error = I()
        self.context = self.lib.clCreateContext(None, 1, c.byref(self.device), None, None, c.byref(error))
        self.check(error.value, "context")
        self.queue = self.lib.clCreateCommandQueue(self.context, self.device, 0, c.byref(error))
        if error.value:
            self.lib.clReleaseContext(self.context)
            self.check(error.value, "command queue")

    @staticmethod
    def check(error, operation):
        if error:
            raise OpenCLError(f"OpenCL {operation} failed ({error})")

    def info_number(self, device, parameter, kind):
        value = kind()
        self.check(self.lib.clGetDeviceInfo(device, parameter, c.sizeof(value), c.byref(value), None), "device info")
        return value.value

    def info_string(self, device, parameter):
        size = Z()
        self.check(self.lib.clGetDeviceInfo(device, parameter, 0, None, c.byref(size)), "device info")
        value = c.create_string_buffer(size.value)
        self.check(self.lib.clGetDeviceInfo(device, parameter, size.value, value, None), "device info")
        return value.value.decode(errors="replace")

    def program(self, source):
        data = source.encode()
        text, size, error = c.c_char_p(data), Z(len(data)), I()
        program = self.lib.clCreateProgramWithSource(self.context, 1, c.byref(text), c.byref(size), c.byref(error))
        self.check(error.value, "program")
        code = self.lib.clBuildProgram(program, 1, c.byref(self.device), b"-cl-std=CL1.2", None, None)
        if code:
            size = Z()
            self.lib.clGetProgramBuildInfo(program, self.device, 0x1183, 0, None, c.byref(size))
            log = c.create_string_buffer(size.value or 1)
            self.lib.clGetProgramBuildInfo(program, self.device, 0x1183, len(log), log, None)
            self.lib.clReleaseProgram(program)
            raise OpenCLError(log.value.decode(errors="replace"))
        return program

    def kernel(self, program, name):
        error = I()
        kernel = self.lib.clCreateKernel(program, name.encode(), c.byref(error))
        self.check(error.value, "kernel")
        return kernel

    def buffer(self, size, data=None):
        if not 0 < size <= 128 * 1024 * 1024:
            raise OpenCLError("Invalid buffer size")
        error = I()
        if data is not None:
            data = bytes(data)
        host = c.create_string_buffer(data, len(data)) if data is not None else None
        if data is not None and len(data) != size:
            raise OpenCLError("Buffer length mismatch")
        buffer = self.lib.clCreateBuffer(self.context, 1 | (32 if host is not None else 0), size, host, c.byref(error))
        self.check(error.value, "buffer")
        return P(buffer)

    def run(self, kernel, arguments, dimensions):
        for index, value in enumerate(arguments):
            self.check(self.lib.clSetKernelArg(kernel, index, c.sizeof(value), c.byref(value)), "argument")
        shape = (Z * len(dimensions))(*dimensions)
        self.check(self.lib.clEnqueueNDRangeKernel(self.queue, kernel, len(dimensions), None, shape, None, 0, None, None), "dispatch")

    def read(self, buffer, size):
        data = c.create_string_buffer(size)
        self.check(self.lib.clEnqueueReadBuffer(self.queue, buffer, 1, 0, size, data, 0, None, None), "readback")
        return data.raw

    def close(self):
        self.lib.clReleaseCommandQueue(self.queue)
        self.lib.clReleaseContext(self.context)
