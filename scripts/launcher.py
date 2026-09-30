"""Own the renderer and QEMU lifetime; use separate persistent guest disks."""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from common import ROOT, WORK, LOGS, command, find_tool, write_json


def prepare_state(state, hidden):
    state.mkdir(parents=True, exist_ok=True)
    identity = state / "device.json"
    expected = {"firmware": hidden.parent.name, "device": "SM-G925F"}
    if identity.exists() and json.loads(identity.read_text()) != expected:
        raise RuntimeError("State directory belongs to another firmware; choose a different --state-dir")
    if not identity.exists() and any(state.glob("*.img")):
        raise RuntimeError("Unrecognized disks in state directory; choose an empty --state-dir")
    write_json(identity, expected)
    for name, mib in (("efs", 64), ("cache", 256), ("userdata", 4096), ("persdata", 64), ("sbfs", 64)):
        target = state / f"{name}.img"
        if target.exists():
            continue
        temporary = target.with_name(target.name + ".partial")
        with temporary.open("wb") as output:
            output.truncate(mib * 1024 * 1024)
        # Explicit features keep new e2fsprogs defaults compatible with 3.10.
        features = "none,has_journal,ext_attr,resize_inode,dir_index,filetype,extent,flex_bg,sparse_super,large_file,huge_file,uninit_bg,dir_nlink,extra_isize"
        command([find_tool("mke2fs"), "-q", "-F", "-t", "ext4", "-b", "4096", "-I", "256", "-O", features,
                 "-E", "lazy_itable_init=0,lazy_journal_init=0", "-L", name, temporary], log="disks-build.log")
        temporary.replace(target)


def stop(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def renderer(mode, frames):
    port = frames.with_suffix(".port")
    modes = ("hardware", "software") if mode == "auto" else (mode,)
    log = LOGS / "renderer.log"
    for choice in modes:
        port.unlink(missing_ok=True)
        environment = dict(os.environ)
        libraries = WORK / "emugl-host/tools/lib64"
        environment["LD_LIBRARY_PATH"] = str(libraries) + ":" + environment.get("LD_LIBRARY_PATH", "")
        if choice == "software":
            environment.update(LIBGL_ALWAYS_SOFTWARE="1", GALLIUM_DRIVER="llvmpipe", __GLX_VENDOR_LIBRARY_NAME="mesa")
        with log.open("ab") as output:
            output.write(f"\nStarting {choice} renderer\n".encode())
            output.flush()
            process = subprocess.Popen([sys.executable, str(ROOT / "scripts/host-gles-renderer.py"),
                                        "--library-dir", str(libraries), "--frames", str(frames)],
                                       env=environment, stdout=output, stderr=subprocess.STDOUT)
        try:
            for attempt in range(100):
                if process.poll() is not None:
                    break
                if frames.exists() and port.exists():
                    time.sleep(0.2)
                    if process.poll() is None:
                        print(f"Renderer initialized ({choice}); details: {log}", flush=True)
                        return process, int(port.read_text().strip())
                time.sleep(0.1)
        except BaseException:
            stop(process)
            raise
        stop(process)
        print(f"Could not initialize {choice} renderer; see {log}", flush=True)
    raise RuntimeError(f"No usable OpenGL renderer. Check your Linux GPU driver and {log}")


def symbols(system_map):
    found = {}
    for line in system_map.read_text().splitlines():
        address, kind, name = line.split()
        if name in ("videomemory", "swapper_pg_dir"):
            found[name] = int(address, 16) - 0xFFFFFFC000000000 + 0x40000000
    if len(found) != 2:
        raise RuntimeError("Kernel System.map does not contain the display symbols")
    return found


def start_audio(runtime):
    port = runtime / "audio.port"
    with (LOGS / "audio-host.log").open("ab") as output:
        process = subprocess.Popen([sys.executable, str(ROOT / "scripts/host-audio-server.py"),
                                    "--port-file", str(port)], stdout=output, stderr=subprocess.STDOUT)
    try:
        for attempt in range(50):
            if process.poll() is not None:
                raise RuntimeError(f"Host audio failed to start; see {LOGS / 'audio-host.log'}")
            if port.exists():
                return process, int(port.read_text().strip())
            time.sleep(0.1)
        raise RuntimeError("Host audio server did not initialize")
    except BaseException:
        stop(process)
        raise


def attach_disk(args, name, image, readonly=False):
    file_node = {"driver": "file", "filename": str(image), "node-name": name + "-file", "read-only": readonly}
    raw_node = {"driver": "raw", "file": name + "-file", "node-name": name, "read-only": readonly}
    args.extend(["-blockdev", json.dumps(file_node), "-blockdev", json.dumps(raw_node),
                 "-device", f"virtio-blk-device,drive={name}"])


def launch(options, qemu, kernel, system, ramdisk, state):
    if not os.environ.get("DISPLAY"):
        raise RuntimeError("The graphics renderer needs an X11 display (XWayland on Wayland). Use --build-only on a headless host.")
    total_ram = next(int(line.split()[1]) // 1024 for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemTotal:"))
    ram = options.ram or min(4096, max(2048, (total_ram // 2 // 256) * 256))
    pointers = symbols(kernel / "System.map")
    with tempfile.TemporaryDirectory(prefix="g925-run-") as directory:
        runtime = Path(directory)
        gpu = vm = audio = None
        try:
            audio, audio_port = start_audio(runtime)
            gpu, port = renderer(options.renderer, runtime / "gpu.shm")
            args = [str(qemu), "-machine", "virt-5.2,gic-version=2,highmem=on",
                    "-accel", "tcg,thread=multi,tb-size=1024", "-cpu", "cortex-a57",
                    "-smp", str(options.cpus), "-m", str(ram), "-kernel", str(kernel / "arch/arm64/boot/Image"),
                    "-initrd", str(ramdisk), "-append",
                    "console=ttyAMA0,115200 loglevel=3 security=selinux "
                    "androidboot.hardware=samsungexynos7420 androidboot.console=ttyAMA0 qemu=1 qemu.gles=1 "
                    "video=vfb:720x1280M-32@60 vfb.videomemorysize=33554432 vfb.qemu_scanout=1 "
                    "test_power.battery_capacity=85 test_power.battery_status=charging test_power.battery_voltage=3800000 "
                    f"androidboot.g925audioport={audio_port}",
                    "-display", "gtk,gl=off,show-cursor=on,zoom-to-fit=on,keep-aspect-ratio=on,show-menubar=off,show-tabs=off,grab-on-hover=off",
                    "-name", "Galaxy S6 edge", "-no-reboot", "-rtc", "base=utc",
                    "-global", "s6-vfb-display.width=720", "-global", "s6-vfb-display.height=1280",
                    "-global", f"s6-vfb-display.ptr-phys={pointers['videomemory']:#x}",
                    "-global", f"s6-vfb-display.pgd-phys={pointers['swapper_pg_dir']:#x}",
                    "-global", f"s6-vfb-display.host-frame-path={runtime / 'gpu.shm'}",
                    "-global", f"g925-goldfish-pipe.port={port}",
                    "-chardev", f"socket,id=console,path={runtime / 'console.sock'},server=on,wait=off,logfile={LOGS / 'serial.log'}",
                    "-serial", "chardev:console", "-qmp", f"unix:{runtime / 'qmp.sock'},server=on,wait=off",
                    "-monitor", f"unix:{runtime / 'monitor.sock'},server=on,wait=off",
                    "-netdev", "user,id=wifi,net=10.0.2.0/24,dhcpstart=10.0.2.15",
                    "-device", "virtio-net-device,netdev=wifi,mac=52:54:00:25:00:01,x-disable-legacy-check=on",
                    "-netdev", f"user,id=adb,net=10.0.3.0/24,dhcpstart=10.0.3.15,restrict=on,hostfwd=tcp:127.0.0.1:{options.adb_port}-10.0.3.15:5555",
                    "-device", "virtio-net-device,netdev=adb,mac=52:54:00:25:00:02,x-disable-legacy-check=on",
                    "-device", "virtio-tablet-device,x-disable-legacy-check=on",
                    "-device", "virtio-keyboard-device,x-disable-legacy-check=on"]
            # MMIO discovery is reversed by this virt machine version. Keep
            # block devices last and reverse them so system is always vda.
            attach_disk(args, "hidden", system.parent / "hidden.raw.img", True)
            for name in ("sbfs", "persdata", "userdata", "cache", "efs"):
                attach_disk(args, name, state / f"{name}.img")
            attach_disk(args, "system", system, True)
            environment = dict(os.environ, G925_HOST_GLES="1", G925_PHONE_NAV="1")
            for name, target in (("g925-console.sock", "console.sock"), ("qemu-g925-qmp.sock", "qmp.sock"), ("qemu-g925-monitor.sock", "monitor.sock")):
                link = WORK / name
                link.unlink(missing_ok=True)
                link.symlink_to(runtime / target)
            with (LOGS / "qemu.log").open("ab") as output:
                vm = subprocess.Popen(args, env=environment, stdout=output, stderr=subprocess.STDOUT)
            write_json(WORK / "session.json", {"pid": vm.pid, "renderer_pid": gpu.pid, "audio_pid": audio.pid, "audio_port": audio_port,
                       "adb_port": options.adb_port, "ram_mib": ram,
                       "cpus": options.cpus, "state_dir": str(state), "console": str(runtime / "console.sock"),
                       "qmp": str(runtime / "qmp.sock"), "command": args})
            print(f"Starting Android: {ram} MiB RAM, {options.cpus} vCPUs, 720×1280 screen.", flush=True)
            print(f"ADB: adb connect 127.0.0.1:{options.adb_port}", flush=True)
            print(f"Logs: {LOGS}\nGuest disks: {state}\nClose the QEMU window to stop.", flush=True)
            while vm.poll() is None:
                if gpu.poll() is not None:
                    raise RuntimeError(f"Graphics renderer exited; see {LOGS / 'renderer.log'}")
                if audio.poll() is not None:
                    raise RuntimeError(f"Audio server exited; see {LOGS / 'audio-host.log'}")
                time.sleep(0.5)
            if vm.returncode:
                raise RuntimeError(f"QEMU exited with code {vm.returncode}; see {LOGS / 'qemu.log'}")
            return 0
        finally:
            stop(vm)
            stop(gpu)
            stop(audio)
            (WORK / "session.json").unlink(missing_ok=True)
            for name in ("g925-console.sock", "qemu-g925-qmp.sock", "qemu-g925-monitor.sock"):
                (WORK / name).unlink(missing_ok=True)
