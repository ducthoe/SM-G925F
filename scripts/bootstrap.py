#!/usr/bin/env python3
"""Build and launch the S6 Edge emulator from its original firmware RAR."""

import argparse
import fcntl
import importlib.util
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

from common import (ROOT, WORK, LOGS, VERSIONS, cached, checkout, command, download,
                    find_tool, fingerprint, unpack_tar, unpack_zip, write_json)
from firmware import prepare_firmware, find_cached_firmware, dump, expand_sparse, debugfs
from supersu import bundle as supersu_bundle


def ensure_dependencies(no_install):
    names = ("git", "curl", "cc", "c++", "make", "patch", "ninja", "pkg-config", "cpio", "ar", "flex", "bison", "bc", "bsdtar")
    missing = [name for name in names if not shutil.which(name)]
    for module in ("venv", "ensurepip"):
        if importlib.util.find_spec(module) is None:
            missing.append("Python " + module)
    for name in ("debugfs", "mke2fs"):
        try:
            find_tool(name)
        except RuntimeError:
            missing.append(name)
    if shutil.which("pkg-config"):
        for name in ("glib-2.0", "pixman-1", "gtk+-3.0", "slirp"):
            if subprocess.run(["pkg-config", "--exists", name]).returncode:
                missing.append(name)
    if not any(shutil.which(name) for name in ("pw-cat", "paplay", "aplay")):
        missing.append("Linux audio playback client")
    if not missing:
        return
    if no_install:
        raise RuntimeError("Missing dependencies: " + ", ".join(missing))
    print("Installing missing host dependencies: " + ", ".join(missing), flush=True)
    command([ROOT / "scripts/install-dependencies.sh"])
    ensure_dependencies(True)


def prepare_tools(downloads):
    gcc = WORK / "toolchain/aarch64-linux-android-4.9"
    if not (gcc / "bin/aarch64-linux-android-gcc").exists():
        # Gitiles archive exports fail with HTTP 503. The mirror contains
        # the exact same pinned compiler commit and preserves symlinks.
        checkout("gcc", gcc)
    ndk = WORK / "toolchain/android-ndk-r25c"
    if not (ndk / "toolchains/llvm/prebuilt/linux-x86_64/bin/clang").exists():
        unpack_zip(download("ndk", downloads), WORK / "toolchain")
    host = WORK / "emugl-host"
    if not (host / "tools/lib64/lib64OpenglRender.so").exists():
        unpack_zip(download("host_gl", downloads), host, "tools/lib64/")
    busybox = WORK / "tools/busybox"
    if not busybox.exists():
        package = download("busybox", downloads)
        data = subprocess.check_output(["ar", "p", str(package), "data.tar.xz"])
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            entry = next(member for member in archive if member.name.endswith("/bin/busybox"))
            busybox.parent.mkdir(parents=True, exist_ok=True)
            busybox.write_bytes(archive.extractfile(entry).read())
            busybox.chmod(0o755)
    return gcc, ndk


def build_qemu(downloads, jobs):
    source = WORK / "qemu-g925-src"
    binary = source / "build/qemu-system-aarch64"
    upstream = VERSIONS["downloads"]["qemu"]["sha256"]
    patch_key = fingerprint(ROOT / "qemu/hw", ROOT / "qemu/patches",
                            extra=upstream + "aarch64-softmmu,gtk,slirp,no-rust")
    configure_args = ["--target-list=aarch64-softmmu", "--disable-docs",
                      "--disable-werror", "--disable-sdl", "--disable-rust",
                      "--enable-gtk", "--enable-slirp", "--enable-lto",
                      "--extra-cflags=-O3 -march=native -mtune=native"]
    # Rebuild if a generated native binary is moved to a different host.
    # CPU frequency and load are deliberately excluded from the cache key.
    cpu_info = Path("/proc/cpuinfo").read_text()
    host_cpu = next((line.partition(":")[2].strip() for line in cpu_info.splitlines()
                     if line.startswith("flags")), "")
    configuration = json.dumps(configure_args) + platform.machine() + host_cpu
    config_key = fingerprint(ROOT / "versions.json", extra=configuration)
    key = patch_key + config_key
    stamp = source / ".g925-build"
    if cached(stamp, key, [binary]):
        print("Using cached patched QEMU.", flush=True)
        return binary
    source_key = source / ".g925-patches"
    if not cached(source_key, patch_key, [source / "configure"]):
        archive = download("qemu", downloads)
        if not cached(source / ".g925-upstream", upstream, [source / "configure"]):
            if source.exists():
                shutil.rmtree(source)
            unpack_tar(archive, source, strip=1)
            (source / ".g925-upstream").write_text(upstream + "\n")
        else:
            # Restore the patched files from the verified archive, then apply
            # the whole series in order. Retain unrelated compiled objects.
            paths = set()
            for patch in (ROOT / "qemu/patches").glob("*.patch"):
                for line in patch.read_text().splitlines():
                    if line.startswith("--- a/"):
                        paths.add(line[6:].split("\t")[0])
            with tarfile.open(archive) as original:
                for path in sorted(paths):
                    member = original.extractfile("qemu-11.1.1/" + path)
                    if member is None:
                        raise RuntimeError(f"Missing pristine QEMU source: {path}")
                    (source / path).write_bytes(member.read())
        for patch in sorted((ROOT / "qemu/patches").glob("*.patch")):
            command(["patch", "--batch", "--forward", "--fuzz=0", "-p1", "-i", patch], cwd=source, log="qemu-build.log")
        for name in ("s6_vfb_display.c", "g925_goldfish_pipe.c"):
            shutil.copyfile(ROOT / "qemu/hw/display" / name, source / "hw/display" / name)
        source_key.write_text(patch_key + "\n")
    print(f"Compiling patched QEMU ({jobs} jobs); log: {LOGS / 'qemu-build.log'}", flush=True)
    config_stamp = source / ".g925-config"
    if not cached(config_stamp, config_key, [source / "build/build.ninja"]):
        command([source / "configure", *configure_args],
                cwd=source, log="qemu-build.log")
        config_stamp.write_text(config_key + "\n")
    command(["ninja", "-C", source / "build", f"-j{jobs}", "qemu-system-aarch64"], log="qemu-build.log")
    stamp.write_text(key + "\n")
    return binary


def build_kernel(gcc, jobs):
    source = WORK / "kernel-source"
    build = WORK / "kernel-old-build"
    wifi = WORK / "wifi"
    key = fingerprint(ROOT / "kernel/g925-virt.patch", ROOT / "kernel/g925-virt.config", ROOT / "kernel/wifi",
                      extra=VERSIONS["sources"]["kernel"]["commit"] + ":rtc-pl031,hctosys,gcc49,modules-v1")
    stamp = build / ".g925-build"
    outputs = [build / "arch/arm64/boot/Image", build / "System.map", wifi / "virtio_net.ko", wifi / "dhd.ko"]
    if cached(stamp, key, outputs):
        print("Using cached kernel.", flush=True)
        return build
    patch_stamp = source / ".g925-patches"
    upstream = VERSIONS["sources"]["kernel"]["commit"]
    source_key = fingerprint(ROOT / "kernel/g925-virt.patch", extra=upstream)
    if not cached(patch_stamp, source_key, [source / ".git"]):
        changed_upstream = not cached(source / ".g925-upstream", upstream, [source / ".git"])
        checkout("kernel", source)
        revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"]).decode().strip()
        if revision != upstream:
            raise RuntimeError("Kernel source does not match its pinned revision")
        # Restore just the patch's files, including files from the previous
        # version. Preserve the source cache and compiled objects so a driver
        # edit needs an incremental build instead of another clone/build.
        previous = source / ".g925-applied.patch"
        patches = [ROOT / "kernel/g925-virt.patch"]
        if previous.exists():
            patches.append(previous)
        paths = {line[6:].split("\t")[0] for patch in patches
                 for line in patch.read_text().splitlines() if line.startswith("+++ b/")}
        for path in sorted(paths):
            pristine = subprocess.run(["git", "-C", str(source), "show", f"HEAD:{path}"], capture_output=True)
            target = source / path
            if pristine.returncode == 0:
                target.write_bytes(pristine.stdout)
            else:
                target.unlink(missing_ok=True)
        command(["git", "-C", source, "apply", ROOT / "kernel/g925-virt.patch"], log="kernel-build.log")
        shutil.copyfile(ROOT / "kernel/g925-virt.patch", previous)
        patch_stamp.write_text(source_key + "\n")
        if changed_upstream and build.exists():
            shutil.rmtree(build)
    build.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "kernel/g925-virt.config", build / ".config")
    # QEMU's PL031 clock gives Android the host date before networking starts.
    command([source / "scripts/config", "--file", build / ".config", "--enable", "RTC_CLASS",
             "--enable", "RTC_HCTOSYS", "--set-str", "RTC_HCTOSYS_DEVICE", "rtc0",
             "--enable", "RTC_DRV_PL031"], log="kernel-build.log")
    prefix = gcc / "bin/aarch64-linux-android-"
    args = ["make", "-C", source, f"O={build}", "ARCH=arm64", f"CROSS_COMPILE={prefix}",
            "HOSTCFLAGS=-O2 -fcommon", "KCFLAGS=-Wno-error", "LDFLAGS_MODULE=", f"-j{jobs}"]
    print(f"Compiling ARM64 kernel ({jobs} jobs); log: {LOGS / 'kernel-build.log'}", flush=True)
    command(args + ["olddefconfig"], log="kernel-build.log")
    command(args + ["Image", "modules_prepare"], log="kernel-build.log")
    wifi.mkdir(exist_ok=True)
    net = (source / "drivers/net/virtio_net.c").read_text()
    (wifi / "virtio_net.c").write_text(net)
    command(["patch", "--batch", "--forward", "--fuzz=0", wifi / "virtio_net.c",
             ROOT / "kernel/wifi/virtio-net-names.patch"], log="kernel-build.log")
    for name in ("dhd.c", "Makefile"):
        shutil.copyfile(ROOT / "kernel/wifi" / name, wifi / name)
    command(args + [f"M={wifi}", "modules"], log="kernel-build.log")
    command([str(prefix) + "strip", "--strip-debug", wifi / "virtio_net.ko", wifi / "dhd.ko"], log="kernel-build.log")
    stamp.write_text(key + "\n")
    return build


def build_graphics(downloads, ndk, firmware, firmware_key, jobs):
    key = fingerprint(ROOT / "scripts/build-compatible-emugl.py", ROOT / "graphics", ROOT / "qemu/wifi_virtual_supplicant.c",
                      extra=firmware_key + VERSIONS["downloads"]["ndk"]["sha256"])
    stamp = WORK / "emugl-compatible/.g925-build"
    outputs = [WORK / "emugl-compatible/lib/hw/gralloc.goldfish.so",
               WORK / "emugl-compatible/lib64/hw/gralloc.goldfish.so", WORK / "wifi/wifi-virtual-supplicant"]
    if cached(stamp, key, outputs):
        print("Using cached graphics drivers.", flush=True)
        return
    for name, destination in (("goldfish", "goldfish-opengl-reference"), ("core", "aosp-core-headers"), ("hardware", "aosp-hardware-headers")):
        checkout(name, WORK / destination)
    drivers = WORK / "emugl-compatible"
    drivers.mkdir(exist_ok=True)
    reference = WORK / "emugl-reference-image"
    if reference.exists():
        shutil.rmtree(reference)
    unpack_zip(download("guest_gl", downloads), reference)
    images = list(reference.rglob("system.img"))
    if len(images) != 1:
        raise RuntimeError("Missing API 21 reference image")
    raw = reference / "system.raw.img"
    expand_sparse(images[0], raw)
    names = ("libGLESv1_enc.so", "libGLESv2_enc.so", "lib_renderControl_enc.so", "libOpenglSystemCommon.so",
             "egl/libEGL_emulation.so", "egl/libGLESv1_CM_emulation.so", "egl/libGLESv2_emulation.so", "hw/gralloc.goldfish.so")
    for name in names:
        target = drivers / "lib" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        dump(raw, "/lib/" + name, target)
    shutil.rmtree(reference)
    stock_libs = WORK / "emugl-compatible-build/stock-libs"
    if stock_libs.exists():
        shutil.rmtree(stock_libs)
    environment = dict(os.environ, G925_NDK=str(ndk), G925_JOBS=str(jobs), G925_STOCK_SYSTEM=str(firmware / "stock.raw.img"))
    print("Compiling compatible Android graphics libraries...", flush=True)
    command([sys.executable, ROOT / "scripts/build-compatible-emugl.py"], env=environment, log="graphics-build.log")
    compiler = ndk / "toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android21-clang"
    command([compiler, "-O2", "-o", WORK / "wifi/wifi-virtual-supplicant", ROOT / "qemu/wifi_virtual_supplicant.c",
             stock_libs / "libcutils.so"], log="graphics-build.log")
    stamp.write_text(key + "\n")


def prepare_images(firmware, firmware_key, *, debloat=False):
    system = firmware / "system-g925emu.img"
    ramdisk = firmware / "ramdisk-g925emu.gz"
    supersu = supersu_bundle()
    system_key = fingerprint(ROOT / "scripts/build-system.py", ROOT / "scripts/firmware.py", ROOT / "scripts/supersu.py", ROOT / "input",
                             *(supersu / name for name in VERSIONS["supersu"]["sha256"]),
                             ROOT / "guest/audio_policy.conf",
                             WORK / "audio/lib/audio.primary.g925emu.so", WORK / "audio/lib64/audio.primary.g925emu.so",
                             WORK / "emugl-compatible", WORK / "compute", WORK / "wifi/dhd.ko", WORK / "wifi/wifi-virtual-supplicant",
                             extra=firmware_key + f":debloat={int(debloat)}")
    stamp = firmware / ".system-build"
    if not cached(stamp, system_key, [system]):
        print("Preparing Android...", flush=True)
        args = [sys.executable, ROOT / "scripts/build-system.py", firmware / "stock.raw.img", system]
        if debloat:
            args.append("--debloat")
        command(args, log="system-build.log")
        stamp.write_text(system_key + "\n")
    ramdisk_key = fingerprint(ROOT / "scripts/build-ramdisk.py", ROOT / "guest", WORK / "wifi/virtio_net.ko", WORK / "wifi/dhd.ko",
                              WORK / "audio/audio-relay", WORK / "audio/g925_headset.ko",
                              WORK / "storage/g925-sdcard",
                              WORK / "input/g925-rotation",
                              WORK / "tools/busybox", extra=firmware_key)
    stamp = firmware / ".ramdisk-build"
    if not cached(stamp, ramdisk_key, [ramdisk]):
        command([sys.executable, ROOT / "scripts/build-ramdisk.py", firmware / "boot/ramdisk", ramdisk], log="ramdisk-build.log")
        stamp.write_text(ramdisk_key + "\n")
    return system, ramdisk


def build_audio(gcc, ndk, jobs, firmware):
    audio = WORK / "audio"
    key = fingerprint(ROOT / "qemu/audio_relay.c", ROOT / "qemu/audio_stock_wrapper.c", ROOT / "kernel/headset", ROOT / "scripts/bootstrap.py",
                      WORK / "kernel-old-build/.g925-build", extra="stock-audio-paced-udp-v3")
    stamp = audio / ".g925-build"
    outputs = [audio / "audio-relay", audio / "g925_headset.ko",
               audio / "lib/audio.primary.g925emu.so", audio / "lib64/audio.primary.g925emu.so"]
    if cached(stamp, key, outputs):
        print("Using cached audio support.", flush=True)
        return
    audio.mkdir(exist_ok=True)
    compiler = ndk / "toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android21-clang"
    command([compiler, "-O2", "-o", audio / "audio-relay", ROOT / "qemu/audio_relay.c"], log="audio-build.log")
    for name in ("g925_headset.c", "Makefile"):
        shutil.copyfile(ROOT / "kernel/headset" / name, audio / name)
    prefix = gcc / "bin/aarch64-linux-android-"
    command(["make", "-C", WORK / "kernel-source", f"O={WORK / 'kernel-old-build'}", f"M={audio}", "ARCH=arm64",
             f"CROSS_COMPILE={prefix}", "HOSTCFLAGS=-O2 -fcommon", "KCFLAGS=-Wno-error", "LDFLAGS_MODULE=",
             f"-j{jobs}", "modules"], log="audio-build.log")
    command([str(prefix) + "strip", "--strip-debug", audio / "g925_headset.ko"], log="audio-build.log")
    # Preserve the firmware's audio stream ABI. The wrapper only disables
    # the stock driver's capture read, which otherwise blocks playback.
    for bits, target in (("lib", "armv7a-linux-androideabi21-clang"), ("lib64", "aarch64-linux-android21-clang")):
        libraries = audio / bits
        libraries.mkdir(exist_ok=True)
        compiler = ndk / "toolchains/llvm/prebuilt/linux-x86_64/bin" / target
        command([compiler, "-shared", "-fPIC", "-O2", "-Wl,-Bsymbolic",
                 "-I" + str(WORK / "aosp-core-headers/include"), "-I" + str(WORK / "aosp-hardware-headers/include"),
                 ROOT / "qemu/audio_stock_wrapper.c", "-ldl", "-o", libraries / "audio.primary.g925emu.so"], log="audio-build.log")
    stamp.write_text(key + "\n")


def build_storage(ndk, firmware, firmware_key):
    source = WORK / "aosp-core-headers/sdcard/sdcard.c"
    header = WORK / "kernel-source/include/uapi/linux/fuse.h"
    storage = WORK / "storage"
    binary = storage / "g925-sdcard"
    key = fingerprint(source, header, ROOT / "scripts/bootstrap.py",
                      extra=firmware_key + VERSIONS["downloads"]["ndk"]["sha256"])
    stamp = storage / ".g925-build"
    if cached(stamp, key, [binary]):
        print("Using cached storage support.", flush=True)
        return
    include = storage / "include/linux"
    include.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(header, include / "fuse.h")
    # Use the kernel's FUSE protocol and the firmware's libcutils ABI.
    for name in ("libcutils.so", "liblog.so"):
        dump(firmware / "stock.raw.img", "/lib64/" + name, storage / name)
    compiler = ndk / "toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android21-clang"
    command([compiler, "-O2", "-Wno-deprecated-declarations", "-DHAVE_SYS_UIO_H", "-DPAGESIZE=4096",
             "-I" + str(storage / "include"), "-I" + str(WORK / "aosp-core-headers/include"),
             source, storage / "libcutils.so", storage / "liblog.so", "-o", binary], log="storage-build.log")
    stamp.write_text(key + "\n")


def build_input(ndk):
    directory = WORK / "input"
    binary = directory / "g925-rotation"
    key = fingerprint(ROOT / "guest/rotation.c", extra=VERSIONS["downloads"]["ndk"]["sha256"])
    stamp = directory / ".g925-build"
    if cached(stamp, key, [binary]):
        return
    directory.mkdir(exist_ok=True)
    compiler = ndk / "toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android21-clang"
    command([compiler, "-O2", "-Wall", "-Wextra", ROOT / "guest/rotation.c", "-o", binary], log="input-build.log")
    stamp.write_text(key + "\n")


def build_compute(ndk, firmware):
    directory = WORK / "compute"
    supported = {
        "lib": "1abca11e7d8b7f58f0a8495383cea4931809c2d0af168e4e01244b2f021ad437",
        "lib64": "d00b1cc10b123bfe4dcdc54fdf137f7cafb506d86adb48b58a09f8094da6e36e",
    }
    key = fingerprint(ROOT / "compute/rs-driver.c",
                      extra=VERSIONS["downloads"]["ndk"]["sha256"] + json.dumps(supported))
    stamp = directory / ".g925-build"
    outputs = [directory / bits / name for bits in supported
               for name in ("libRSDriver.so", "libRSDriver.stock.so")]
    if cached(stamp, key, outputs):
        print("Using cached compute support.", flush=True)
        return
    from common import digest
    for bits, target in (("lib", "armv7a-linux-androideabi21-clang"),
                         ("lib64", "aarch64-linux-android21-clang")):
        libraries = directory / bits
        libraries.mkdir(parents=True, exist_ok=True)
        stock = libraries / "libRSDriver.stock.so"
        dump(firmware / "stock.raw.img", f"/{bits}/libRSDriver.so", stock)
        if digest(stock) != supported[bits]:
            raise RuntimeError(f"Unsupported {bits} RenderScript driver")
        compiler = ndk / "toolchains/llvm/prebuilt/linux-x86_64/bin" / target
        command([compiler, "-shared", "-fPIC", "-O2", "-Wall", "-Wextra",
                 "-Wl,-soname,libRSDriver.so", ROOT / "compute/rs-driver.c",
                 "-ldl", "-llog", "-lm", "-o", libraries / "libRSDriver.so"], log="compute-build.log")
    stamp.write_text(key + "\n")


def load_debloat(state, firmware):
    options = state / "options.json"
    if options.exists():
        try:
            value = json.loads(options.read_text())["debloat"]
        except (ValueError, KeyError, TypeError) as error:
            raise RuntimeError(f"Invalid phone options: {options}; use --debloat or --no-debloat to reset them") from error
        if not isinstance(value, bool):
            raise RuntimeError(f"Invalid debloat option in {options}; use --debloat or --no-debloat to reset it")
        return value
    # Migrate phones created before options were saved. The supported stock
    # image contains S Health, which the minimal profile always removes.
    system = firmware / "system-g925emu.img"
    if (state / "device.json").exists() and system.exists():
        try:
            debugfs(system, "stat /priv-app/SHealth4")
        except RuntimeError as error:
            if "File not found" in str(error):
                return True
            raise
    return False


def save_debloat(state, debloat):
    options = state / "options.json"
    value = {"debloat": debloat}
    if not options.exists() or options.read_text() != json.dumps(value, indent=2) + "\n":
        write_json(options, value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("firmware", nargs="?", type=Path,
                        help="SM-G925F repair firmware .rar; omit to reuse the extracted firmware cache")
    parser.add_argument("--firmware-id", help="select an extracted firmware cache by its ID or SHA-256 prefix")
    parser.add_argument("--build-only", action="store_true", help="prepare everything without opening a VM")
    parser.add_argument("--no-install", action="store_true", help="report missing packages without sudo")
    parser.add_argument("--debloat", action=argparse.BooleanOptionalAction, default=None,
                        help="enable or disable minimal apps; remembers the choice for this phone (new phones: off)")
    parser.add_argument("--renderer", choices=("auto", "hardware", "software"), default="auto")
    parser.add_argument("--adb-port", type=int, default=5555,
                        help="localhost ADB port (default: 5555)")
    parser.add_argument("--ram", type=int, help="guest RAM in MiB (up to 4096 by default)")
    parser.add_argument("--cpus", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--jobs", type=int, default=min(6, os.cpu_count() or 1))
    parser.add_argument("--downloads", type=Path, default=ROOT / "downloads")
    parser.add_argument("--state-dir", type=Path, help="separate writable guest disks")
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        parser.error("This version supports x86_64 Linux hosts")
    if sys.version_info < (3, 10):
        parser.error("Python 3.10 or newer is required")
    if args.firmware is not None and args.firmware_id:
        parser.error("Provide either a firmware .rar or --firmware-id, not both")
    if args.firmware is not None and (not args.firmware.is_file() or args.firmware.suffix.lower() != ".rar"):
        parser.error("Provide an existing firmware .rar file")
    if args.jobs < 1 or not 1 <= args.cpus <= 8 or (args.ram is not None and not 2048 <= args.ram <= 4096):
        parser.error("Use jobs >= 1, 1–8 CPUs, and 2048–4096 MiB RAM")
    if not 1 <= args.adb_port <= 65535:
        parser.error("Use an ADB port between 1 and 65535")
    if any(character.isspace() for character in str(ROOT)):
        parser.error("Put this checkout in a path without whitespace (legacy kernel Makefiles require it)")
    if not args.build_only and not os.environ.get("DISPLAY"):
        parser.error("Open a terminal in your Linux desktop, or use --build-only to prepare files without a window")
    for path in (WORK, LOGS, args.downloads):
        path.mkdir(parents=True, exist_ok=True)
    with (WORK / "launcher.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("This checkout already has a build or emulator running")
        if args.firmware is None:
            firmware, firmware_key = find_cached_firmware(args.firmware_id)
        ensure_dependencies(args.no_install)
        if args.firmware is not None:
            firmware, firmware_key = prepare_firmware(args.firmware.resolve())
        from launcher import prepare_state, launch
        state = args.state_dir.resolve() if args.state_dir else ROOT / "state" / firmware_key[:16]
        debloat = load_debloat(state, firmware) if args.debloat is None else args.debloat
        prepare_state(state, firmware / "hidden.raw.img")
        save_debloat(state, debloat)
        gcc, ndk = prepare_tools(args.downloads.resolve())
        qemu = build_qemu(args.downloads.resolve(), args.jobs)
        kernel = build_kernel(gcc, args.jobs)
        build_graphics(args.downloads.resolve(), ndk, firmware, firmware_key, args.jobs)
        build_audio(gcc, ndk, args.jobs, firmware)
        build_storage(ndk, firmware, firmware_key)
        build_input(ndk)
        build_compute(ndk, firmware)
        system, ramdisk = prepare_images(firmware, firmware_key, debloat=debloat)
        print("Build complete.", flush=True)
        if not args.build_only:
            return launch(args, qemu, kernel, system, ramdisk, state)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("Interrupted; completed build stages remain cached.", file=sys.stderr)
        raise SystemExit(130)
