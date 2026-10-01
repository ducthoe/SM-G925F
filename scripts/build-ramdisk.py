#!/usr/bin/env python3
"""Create the portable init overlay from the extracted Samsung boot ramdisk."""

import argparse
import gzip
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from common import ROOT, WORK, safe_name

ALIASES = {"SYSTEM": "vda", "EFS": "vdb", "CACHE": "vdc", "USERDATA": "vdd",
           "PERSDATA": "vde", "SBFS": "vdf", "HIDDEN": "vdg"}
SERVICES = {
    "init.rc": ("icd", "ril-daemon", "SMD-daemon", "DR-daemon", "BCS-daemon", "at_distributor"),
    "init.baseband.rc": ("cpboot-daemon", "DIAG-daemon"),
    "init.gps.rc": ("lhd", "gpsd"),
    # The emulator kernel has no Samsung SDP netlink endpoint. This daemon
    # exits immediately and init otherwise retries it every five seconds.
    "init.container.rc": ("sdp_cryptod",),
    "init.samsungexynos7420.rc": ("argos-daemon", "ipsec-daemon", "mobicore", "secure_storage", "watchdogd",
                                 "vcsFPService", "bauthserver"),
}


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise RuntimeError(f"Unsupported ramdisk: expected one {old!r}")
    return text.replace(old, new)


def build(source, output):
    with tempfile.TemporaryDirectory(prefix="g925-ramdisk-", dir=WORK) as directory:
        tree = Path(directory)
        archive = gzip.decompress(source.read_bytes())
        listing = subprocess.check_output(["cpio", "-it", "--quiet"], input=archive).decode()
        for name in listing.splitlines():
            safe_name(name)
        subprocess.run(["cpio", "-idm", "--quiet", "--no-absolute-filenames", "--no-preserve-owner"],
                       input=archive, cwd=tree, check=True)
        init = tree / "init.rc"
        init.write_text(replace_once(init.read_text(), "import /init.${ro.hardware}.rc\n",
                                    "import /init.${ro.hardware}.rc\nimport /init.g925emu.rc\n"))
        properties = tree / "default.prop"
        properties.write_text(replace_once(properties.read_text(), "ro.adb.secure=1\n",
                                            "ro.adb.secure=0\n") + "service.adb.tcp.port=5555\n")
        # The virtual phone has no USB gadget. USB mode transitions must
        # not stop the TCP daemon used by the isolated debugging link.
        for rc in tree.glob("init*.usb.rc"):
            rc.write_text(re.sub(r"(?m)^    stop adbd\n", "", rc.read_text()))
        for name, services in SERVICES.items():
            rc = tree / name
            text = rc.read_text()
            for service in services:
                text, count = re.subn(rf"(?m)^(service {re.escape(service)} [^\n]*)\n",
                                      r"\1\n    disabled\n", text)
                if count != 1:
                    raise RuntimeError(f"Unsupported ramdisk service: {service}")
                text = re.sub(rf"(?m)^    start {re.escape(service)}\n", "", text)
            rc.write_text(text)
        fstab = tree / "fstab.samsungexynos7420"
        fstab.write_text(replace_once(fstab.read_text(), "wait,support_scfs,verify", "wait"))
        hardware = tree / "init.samsungexynos7420.rc"
        hardware.write_text(replace_once(hardware.read_text(),
            "service sdcard /system/bin/sdcard -u 1023 -g 1023 -l -r /data/media /mnt/shell/emulated\n"
            "    class late_start\n    oneshot\n",
            "service sdcard /sbin/g925-sdcard -u 1023 -g 1023 -l /data/media /mnt/shell/emulated\n"
            "    class late_start\n    seclabel u:r:sdcardd:s0\n"))
        uevent = tree / "ueventd.rc"
        uevent.write_text(uevent.read_text() + "\n/dev/goldfish_pipe 0666 root root\n")
        (tree / "sbin").mkdir(exist_ok=True)
        files = {"busybox": WORK / "tools/busybox", "virtio_net.ko": WORK / "wifi/virtio_net.ko",
                 "dhd.ko": WORK / "wifi/dhd.ko", "g925-first-boot.sh": ROOT / "guest/first-boot.sh",
                 "adb-start.sh": ROOT / "guest/adb-start.sh",
                 "g925_headset.ko": WORK / "audio/g925_headset.ko", "audio-relay": WORK / "audio/audio-relay",
                 "audio-start.sh": ROOT / "guest/audio-start.sh", "g925-sdcard": WORK / "storage/g925-sdcard",
                 "g925-rotation": WORK / "input/g925-rotation"}
        for name, original in files.items():
            target = tree / "sbin" / name
            shutil.copyfile(original, target)
            target.chmod(0o644 if name.endswith(".ko") else 0o755)
        lines = ["# Portable QEMU devices and Samsung init aliases.", "on init",
                 "    mkdir /dev/block/platform 0755 root root",
                 "    mkdir /dev/block/platform/15570000.ufs 0755 root root",
                 "    mkdir /dev/block/platform/15570000.ufs/by-name 0755 root root",
                 "    mkdir /dev/graphics 0755 root root", "    symlink /dev/fb0 /dev/graphics/fb0",
                 "    symlink /dev/goldfish_pipe /dev/qemu_pipe"]
        for name, disk in ALIASES.items():
            lines.append(f"    symlink /dev/block/{disk} /dev/block/platform/15570000.ufs/by-name/{name}")
        lines.extend(["", "on early-init", "    setprop ro.radio.noril yes", "",
                      "# Stock services may advertise unsupported MDPP hardware at startup.",
                      "on property:security.mdpp=Ready", "    setprop security.mdpp None", "",
                      "on post-fs-data", "    setprop security.mdpp None",
                      "    insmod /sbin/virtio_net.ko", "    insmod /sbin/dhd.ko",
                      "    insmod /sbin/g925_headset.ko", "    start g925audio", "    start g925supersu",
                      "    start g925adb",
                      "    chmod 0666 /sys/module/dhd/parameters/firmware_path",
                      "    chmod 0666 /sys/module/dhd/parameters/nvram_path",
                      "    setprop wlan.driver.status ok", "", "on boot",
                      "    start g925console", "    start g925firstboot", "    start g925rotate", "",
                      "service g925console /sbin/busybox sh -i", "    disabled", "    console",
                      "    seclabel u:r:init:s0", "",
                      "service g925firstboot /sbin/busybox sh /sbin/g925-first-boot.sh",
                      "    disabled", "    oneshot", "    seclabel u:r:init:s0", "",
                      "service g925rotate /sbin/g925-rotation",
                      "    disabled", "    seclabel u:r:init:s0", "",
                      "service g925audio /sbin/busybox sh /sbin/audio-start.sh",
                      "    disabled", "    seclabel u:r:init:s0", "",
                      "service g925adb /sbin/busybox sh /sbin/adb-start.sh",
                      "    disabled", "    oneshot", "    seclabel u:r:init:s0", "",
                      "service g925supersu /system/xbin/daemonsu --auto-daemon",
                      "    class main", "    user root", "    group root",
                      "    disabled", "    oneshot", "    seclabel u:r:init:s0"])
        stop = [service for services in SERVICES.values() for service in services]
        stop.extend(("powersnd", "SIDESYNC_service", "ss_kb_service", "macloader"))
        for service in stop:
            lines.extend(["", f"on property:init.svc.{service}=running", f"    stop {service}"])
        (tree / "init.g925emu.rc").write_text("\n".join(lines) + "\n")
        (tree / "init.g925emu.rc").chmod(0o644)
        names = "".join("./" + str(path.relative_to(tree)) + "\n" for path in sorted(tree.rglob("*")))
        result = subprocess.check_output(["cpio", "-o", "-H", "newc", "--quiet", "--owner=0:0"],
                                         input=names.encode(), cwd=tree)
        temporary = output.with_name(output.name + ".partial")
        temporary.write_bytes(gzip.compress(result, mtime=0))
        temporary.replace(output)
    print(f"Prepared {output}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.source, args.output)
