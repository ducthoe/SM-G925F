# Galaxy S6 Edge emulator

**Samsung Android 5.0.2 with TouchWiz, running in a Linux desktop window.**

Supply your firmware RAR and run one script. It downloads the tools, builds patched QEMU and the kernel, prepares Android, and opens the phone.

**Status: preview.** See [known limitations](#known-limitations) before using it.

## What you need

- A **Linux PC with an Intel or AMD processor** (`x86_64`). Windows, macOS, and ARM hosts are currently unsupported.
- A graphical desktop: X11, or Wayland with XWayland.
- **Python 3.10 or newer**, internet access for the first build, and `sudo` access to install missing packages.
- Recommended: **8 GB RAM and 30 GB free disk space**.
- Your own copy of this firmware:

  ```text
  G925FXXU1AOCV_OXE1AOD1_v5.0.2_Repair_Firmware.rar
  ```

Only the **SM-G925F G925FXXU1AOCV Android 5.0.2 repair RAR**, containing AP and CSC packages, is supported. Other S6 variants and firmware versions will be rejected. Firmware is not included.

## Quick start

### 1. Get the project

[Download the source ZIP](https://github.com/ducthoe/SM-G925F/archive/refs/heads/main.zip), extract it, and open a terminal inside the extracted folder. Keep the folder path free of spaces.

Or, if you have Git:

```sh
git clone https://github.com/ducthoe/SM-G925F.git
cd SM-G925F
```

### 2. Start the emulator

```sh
chmod +x run.sh scripts/install-dependencies.sh
./run.sh "/path/to/G925FXXU1AOCV_OXE1AOD1_v5.0.2_Repair_Firmware.rar"
```

Replace `/path/to/` with the location of your RAR. **Leave it compressed.** Run the command as your normal desktop user; the installer asks for your password when it needs `sudo`.

The first run downloads and compiles several large components, so it takes longer. Progress appears in the terminal. Missing packages are installed automatically on Debian/Ubuntu, Fedora, and Arch Linux.

### 3. Use Android

The phone opens in a window that fits the whole screen. A fresh device starts in English and skips setup.

| Action | Control |
| --- | --- |
| Tap | Left click |
| Swipe | Hold the left mouse button and drag |
| Recent apps / Home / Back | Buttons below the phone screen |
| Resize | Drag the window edges; the screen scales to fit |
| Internet | Enable **QEMU Wi-Fi** in Android's Wi-Fi settings |
| Sound | Use your Linux speakers or headphones and Android's volume controls |
| Root access | Open **SuperSU** in the app drawer |
| Stop | Close the QEMU window |

## Run it again

Use the **same command**. Completed builds are reused, and your apps and settings are saved in `state/`.

Keep `state/` when updating the project. To try a separate, fresh phone without deleting your existing data:

```sh
./run.sh "/path/to/firmware.rar" --state-dir "$PWD/state/fresh-phone"
```

## Useful options

Add these after the firmware path:

| Option | Use it to… |
| --- | --- |
| `--ram 4096 --cpus 4` | Assign 4 GB RAM and four virtual CPUs |
| `--renderer software` | Use software graphics if your GPU driver fails |
| `--jobs 2` | Reduce memory use while compiling |
| `--build-only` | Download and build without opening Android |
| `--no-install` | Report missing dependencies without installing packages |
| `--help` | Show all options |

The default is up to **4 GB RAM and four virtual CPUs**. CPU emulation and software graphics can limit performance.

## Common problems

**The build failed:** read the error and the log path printed in the terminal. Fix the reported problem and run the same command again; completed stages are kept. Package lists are in [the dependency installer](scripts/install-dependencies.sh).

**The screen is black or graphics initialization fails:** check `logs/renderer.log`, then try `--renderer software`. Launch from a terminal in your graphical desktop.

**There is no internet:** turn on Wi-Fi in Android and connect to **QEMU Wi-Fi**. It uses your PC's internet connection; it does not scan nearby physical access points.

**There is no sound:** check Android's volume, the selected Linux audio output, and `logs/audio-host.log`. Playback supports PipeWire, PulseAudio, or ALSA.

**It says another emulator is running:** this checkout allows one session at a time. Close its existing window before starting another.

When reporting a problem, include your Linux distribution, GPU, launch command, and the relevant log. Keep firmware and phone data out of bug reports.

## Known limitations

- Development has been verified on **Debian 13 with an NVIDIA Quadro M2000M**. Intel/AMD graphics and the Fedora/Arch installers still need verification on those hosts.
- Some Samsung icons and effects render incorrectly. Performance depends on your PC.
- Camera, cellular calls, NFC, physical sensors, and microphone input are unavailable.
- This runs an older Android release and includes SuperSU 2.49.

## For contributors

Build scripts are in `scripts/`, QEMU changes in `qemu/`, and the kernel port in `kernel/`. Downloads and generated files stay in `downloads/` and `working/`; phone data stays in `state/`.

The original firmware is preserved. Source revisions and checksums are recorded in `versions.json`. Upstream components retain their own licenses; see [source and license notices](NOTICE.md).
