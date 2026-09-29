#!/usr/bin/env python3
"""Build ARM64 EmuGL with the legacy 96-byte cross-ABI buffer layout."""

import concurrent.futures
import os
import subprocess
from pathlib import Path

from firmware import dump


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "working/goldfish-opengl-reference/opengl"
BUILD = ROOT / "working/emugl-compatible-build"
OUTPUT = ROOT / "working/emugl-compatible/lib64"
NDK = Path(os.environ["G925_NDK"]) / "toolchains/llvm/prebuilt/linux-x86_64"


def main() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    (BUILD / "include/ui").mkdir(parents=True, exist_ok=True)
    pixel_format = ROOT / "graphics/include/ui/PixelFormat.h"
    (BUILD / "include/ui/PixelFormat.h").write_bytes(pixel_format.read_bytes())
    for name in ("bionic_tls.h", "bionic_macros.h"):
        (BUILD / "include" / name).write_bytes((ROOT / "graphics/include" / name).read_bytes())
    (BUILD / "include/__get_tls.h").write_text(
        "#pragma once\nstatic inline void **__get_tls() { "
        "return (void **)__builtin_thread_pointer(); }\n")
    include_dirs = [
        BUILD / "include", ROOT / "working/aosp-core-headers/include",
        ROOT / "working/aosp-hardware-headers/include", SOURCE / "host/include",
        SOURCE / "host/include/libOpenglRender",
        SOURCE / "shared/OpenglCodecCommon",
        *[SOURCE / "system" / name for name in (
            "OpenglSystemCommon", "GLESv1_enc", "GLESv2_enc", "renderControl_enc")],
    ]
    compiler = str(NDK / "bin/aarch64-linux-android21-clang++")
    flags = ["-std=gnu++11", "-fPIC", "-O2", "-fno-exceptions", "-fno-rtti",
             "-DLOG_TAG=\"G925EmuGL\"", "-DGL_GLEXT_PROTOTYPES", "-DHAVE_PTHREADS",
             "-DHAVE_SYS_UIO_H", "-DHAVE_ANDROID_OS",
             "-DEGL_EGLEXT_PROTOTYPES", "-DWITH_GLES2", "-Wno-deprecated-declarations",
             "-include", "cutils/log.h", "-include", "string.h",
             *["-I" + str(path) for path in include_dirs]]
    groups = {
        "libGLESv1_enc.so": list((SOURCE / "system/GLESv1_enc").glob("*.cpp")),
        "libGLESv2_enc.so": list((SOURCE / "system/GLESv2_enc").glob("*.cpp")),
        "lib_renderControl_enc.so": list((SOURCE / "system/renderControl_enc").glob("*.cpp")),
        "libOpenglSystemCommon.so": list((SOURCE / "system/OpenglSystemCommon").glob("*.cpp")),
        "egl/libEGL_emulation.so": list((SOURCE / "system/egl").glob("*.cpp")),
        "egl/libGLESv1_CM_emulation.so": [SOURCE / "system/GLESv1/gl.cpp"],
        "egl/libGLESv2_emulation.so": [SOURCE / "system/GLESv2/gl2.cpp"],
        "hw/gralloc.goldfish.so": [SOURCE / "system/gralloc/gralloc.cpp"],
    }
    common = [SOURCE / "shared/OpenglCodecCommon" / name for name in (
        "GLClientState.cpp", "GLSharedGroup.cpp", "glUtils.cpp",
        "SocketStream.cpp", "TcpStream.cpp", "TimeUtils.cpp")]
    sources = sorted(set(common + [path for paths in groups.values() for path in paths]))
    objects = {path: BUILD / (str(path.relative_to(SOURCE)).replace("/", "_") + ".o")
               for path in sources}

    def compile_source(path: Path) -> None:
        compile_path = path
        if path.name == "GL2Encoder.cpp":
            compile_path = BUILD / "GL2Encoder.cpp"
            compile_path.write_text(path.read_text().replace(
                "char *brace = strrchr(name,'[');", "const char *brace = strrchr(name,'[');"))
        elif path.name == "gralloc.cpp":
            compile_path = BUILD / "gralloc.cpp"
            compile_path.write_text(path.read_text().replace(
                "intptr_t *postCountPtr = (intptr_t *)", "uint32_t *postCountPtr = (uint32_t *)")
                .replace("sizeof(intptr_t)", "sizeof(uint32_t)"))
        subprocess.run([compiler, *flags, "-c", str(compile_path), "-o", str(objects[path])],
                       check=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=int(os.environ.get("G925_JOBS", "4"))) as pool:
        for _ in pool.map(compile_source, sources):
            pass
    libs = BUILD / "stock-libs"
    libs.mkdir(exist_ok=True)
    for name in ("libcutils.so", "libutils.so", "liblog.so"):
        target = libs / name
        if not target.exists():
            dump(Path(os.environ["G925_STOCK_SYSTEM"]), f"/lib64/{name}", target)
    for name, paths in groups.items():
        target = OUTPUT / name
        target.parent.mkdir(parents=True, exist_ok=True)
        dependencies = []
        if name == "libOpenglSystemCommon.so":
            dependencies = ["libGLESv1_enc.so", "libGLESv2_enc.so", "lib_renderControl_enc.so"]
        elif "/" in name:
            dependencies = ["libOpenglSystemCommon.so", "libGLESv1_enc.so",
                            "libGLESv2_enc.so", "lib_renderControl_enc.so"]
        subprocess.run([compiler, "-shared", "-static-libstdc++",
            "-Wl,-soname," + target.name, "-o", str(target),
            *[str(objects[path]) for path in paths + common],
            *[str(OUTPUT / item) for item in dependencies],
            *[str(libs / item) for item in ("libcutils.so", "libutils.so", "liblog.so")],
            "-ldl", "-lm"], check=True)
        print("Built", target.relative_to(ROOT), flush=True)


if __name__ == "__main__":
    main()
