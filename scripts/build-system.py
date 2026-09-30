#!/usr/bin/env python3
"""Make one adapted image while preserving the extracted stock filesystem."""

import argparse
import os
import re
import tempfile
from pathlib import Path

from common import ROOT, WORK, command, digest
from firmware import SUPPORTED_SERVICES, SUPPORTED_SURFACEFLINGER, debugfs, dump, quote
from supersu import bundle as supersu_bundle


def exists(image, path):
    try:
        debugfs(image, f"stat {quote(path)}")
        return True
    except RuntimeError as error:
        if "File not found" in str(error):
            return False
        raise


def remove_tree(image, path):
    if not exists(image, path):
        return
    for line in debugfs(image, f"ls -p {quote(path)}").splitlines():
        fields = line.split("/")
        if len(fields) < 7 or fields[5] in (".", "..", ""):
            continue
        child = path + "/" + fields[5]
        if int(fields[2], 8) & 0o170000 == 0o040000:
            remove_tree(image, child)
        else:
            debugfs(image, f"rm {quote(child)}", write=True)
    debugfs(image, f"rmdir {quote(path)}", write=True)


def install(image, source, target, label, *, executable=False):
    parent = str(Path(target).parent)
    if not exists(image, parent):
        debugfs(image, f"mkdir {quote(parent)}", write=True)
    if exists(image, target):
        debugfs(image, f"rm {quote(target)}", write=True)
    debugfs(image, f"write {quote(source)} {quote(target)}", write=True)
    for name, value in (("mode", "0100755" if executable else "0100644"), ("uid", "0"), ("gid", "0")):
        debugfs(image, f"set_inode_field {quote(target)} {name} {value}", write=True)
    debugfs(image, f"ea_set -f {quote(label)} {quote(target)} security.selinux", write=True)


def patch_instruction(data, offset, before, after):
    if data[offset:offset + len(before)] != before:
        raise RuntimeError(f"Unexpected firmware instruction at {offset:#x}")
    data[offset:offset + len(before)] = after


def build(source, output):
    temporary = output.with_name(output.name + ".partial")
    command(["cp", "--reflink=auto", "--sparse=always", source, temporary])
    with tempfile.TemporaryDirectory(prefix="g925-system-", dir=WORK) as directory:
        temp = Path(directory)
        library_label = temp / "library-label"
        library_label.write_bytes(b"u:object_r:system_library_file:s0\0")
        file_label = temp / "file-label"
        file_label.write_bytes(b"u:object_r:system_file:s0\0")
        wpa_label = temp / "wpa-label"
        wpa_label.write_bytes(b"u:object_r:wpa_exec:s0\0")

        surfaceflinger = temp / "libsurfaceflinger.so"
        dump(source, "/lib64/libsurfaceflinger.so", surfaceflinger)
        if digest(surfaceflinger) != SUPPORTED_SURFACEFLINGER:
            raise RuntimeError("Unsupported SurfaceFlinger binary")
        data = bytearray(surfaceflinger.read_bytes())
        for offset in (0x2B9BC, 0x2CE80, 0x2DFE8, 0x3106C, 0x31298, 0x31780, 0x3FAAC):
            if data[offset + 1:offset + 4] != bytes.fromhex("1140f9"):
                raise RuntimeError(f"Unexpected HWC lookup at {offset:#x}")
            data[offset + 1] = 0x15
        patch_instruction(data, 0x2BA34, bytes.fromhex("080040f9"), bytes.fromhex("03000014"))
        for offset, before in ((0x2CF08, "fab9ff97"), (0x2E078, "9eb5ff97"), (0x31108, "7aa9ff97")):
            patch_instruction(data, offset, bytes.fromhex(before), bytes.fromhex("1f2003d5"))
        surfaceflinger.write_bytes(data)
        install(temporary, surfaceflinger, "/lib64/libsurfaceflinger.so", library_label)

        services = temp / "services.odex"
        dump(source, "/framework/arm64/services.odex", services)
        if digest(services) != SUPPORTED_SERVICES:
            raise RuntimeError("Unsupported precompiled services.odex")
        data = bytearray(services.read_bytes())
        if data[0x239757C:0x2397580] != bytes.fromhex("80978252"):
            raise RuntimeError("Unexpected USB exception path")
        patch_instruction(data, 0x2397570, bytes.fromhex("350300b5"), bytes.fromhex("19000014"))
        services.write_bytes(data)
        install(temporary, services, "/framework/arm64/services.odex", file_label)

        # Keep the original firmware archive and extracted stock image intact.
        for bits in ("lib", "lib64"):
            remove_tree(temporary, f"/vendor/{bits}/egl")
            for name in ("gralloc.exynos5.so", "hwcomposer.exynos5.so"):
                target = f"/{bits}/hw/{name}"
                if exists(temporary, target):
                    debugfs(temporary, f"rm {quote(target)}", write=True)
        audio = "/lib/hw/audio.primary.universal7420.so"
        if exists(temporary, audio):
            debugfs(temporary, f"rm {audio}", write=True)

        drivers = WORK / "emugl-compatible"
        libraries = sorted(drivers.rglob("*.so"))
        if len(libraries) != 16:
            raise RuntimeError("Expected both sets of eight compatible GLES libraries")
        for library in libraries:
            install(temporary, library, "/" + str(library.relative_to(drivers)), library_label)
        config = temp / "egl.cfg"
        config.write_text("0 0 emulation\n")
        install(temporary, config, "/lib/egl/egl.cfg", library_label)
        install(temporary, ROOT / "input/Vendor_0627_Product_0003.idc",
                "/usr/idc/Vendor_0627_Product_0003.idc", file_label)
        keymap = temp / "Generic.kl"
        dump(source, "/usr/keylayout/Generic.kl", keymap)
        text = keymap.read_text()
        if not re.search(r"(?m)^key\s+254\s+APP_SWITCH", text):
            text += "\nkey 254 APP_SWITCH\n"
        keymap.write_text(text)
        install(temporary, keymap, "/usr/keylayout/Generic.kl", file_label)

        properties = temp / "build.prop"
        dump(source, "/build.prop", properties)
        text = properties.read_text()
        updates = {"ro.sf.lcd_density": "320", "ro.opengles.version": "131072",
                   "ro.hardware.gralloc": "goldfish", "ro.product.locale.language": "en",
                   "ro.product.locale.region": "GB", "ro.radio.noril": "yes"}
        for key, value in updates.items():
            pattern = rf"(?m)^{re.escape(key)}=.*$"
            if re.search(pattern, text):
                text = re.sub(pattern, f"{key}={value}", text)
            else:
                text += f"\n{key}={value}\n"
        properties.write_text(text)
        install(temporary, properties, "/build.prop", file_label)
        install(temporary, WORK / "wifi/wifi-virtual-supplicant", "/bin/wpa_supplicant", wpa_label, executable=True)
        install(temporary, WORK / "wifi/dhd.ko", "/lib/modules/dhd.ko", file_label)
        for bits in ("lib", "lib64"):
            audio = WORK / "audio" / bits / "audio.primary.g925emu.so"
            install(temporary, audio, f"/{bits}/hw/audio.primary.default.so", library_label)
        install(temporary, ROOT / "guest/audio_policy.conf", "/etc/audio_policy.conf", file_label)

        # The pinned kernel ships matching ARM64 SuperSU binaries and UI.
        # Init starts the daemon directly, so Android's app_process is retained.
        supersu = supersu_bundle()
        for name in ("su", "daemonsu", "sugote"):
            install(temporary, supersu / "su", f"/xbin/{name}", file_label, executable=True)
        install(temporary, supersu / "su", "/bin/.ext/.su", file_label, executable=True)
        install(temporary, supersu / "supolicy", "/xbin/supolicy", file_label, executable=True)
        install(temporary, supersu / "libsupol.so", "/lib64/libsupol.so", library_label)
        install(temporary, supersu / "SuperSU.apk", "/app/SuperSU/SuperSU.apk", file_label)
        shell = temp / "sugote-mksh"
        dump(source, "/bin/sh", shell)
        install(temporary, shell, "/xbin/sugote-mksh", file_label, executable=True)
        marker = temp / "installed-su-daemon"
        marker.write_text("1\n")
        install(temporary, marker, "/etc/.installed_su_daemon", file_label)

        # Persistent IMS processes ignore force-stop; omit their APKs from the
        # emulator copy so they cannot restart and display modem crash dialogs.
        for path in ("/app/imsservice", "/app/ImsTelephonyService", "/app/ImsSettings",
                     "/priv-app/ImsLogger+", "/app/NfcNci", "/priv-app/SetupWizard",
                     "/app/SecSetupWizard2015", "/app/KnoxSetupWizardClient"):
            remove_tree(temporary, path)
        # Samsung's Device Test (com.sec.factory) probes physical hardware
        # that this virtual phone does not have.
        remove_tree(temporary, "/priv-app/DeviceTest")
    temporary.replace(output)
    print(f"Prepared {output}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.source, args.output)
