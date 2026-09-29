#!/usr/bin/env bash
# Install host tools through the host distribution's package manager.
set -euo pipefail
if [[ $EUID == 0 ]]; then
    elevate=()
else
    elevate=(sudo)
fi
if command -v apt-get >/dev/null; then
    "${elevate[@]}" apt-get update
    "${elevate[@]}" apt-get install -y build-essential patch git curl ca-certificates \
        python3 python3-venv python3-pip ninja-build pkg-config meson \
        libglib2.0-dev libpixman-1-dev libgtk-3-dev libslirp-dev libfdt-dev \
        zlib1g-dev libffi-dev libssl-dev libepoxy-dev libgl1 libegl1 \
        libgl1-mesa-dri libx11-6 cpio e2fsprogs libarchive-tools \
        flex bison bc xz-utils unzip pipewire-bin pulseaudio-utils alsa-utils
elif command -v dnf >/dev/null; then
    "${elevate[@]}" dnf install -y gcc gcc-c++ make patch git curl ca-certificates \
        python3 python3-pip ninja-build pkgconf-pkg-config meson \
        glib2-devel pixman-devel gtk3-devel libslirp-devel libfdt-devel \
        zlib-devel libffi-devel openssl-devel libepoxy-devel \
        mesa-libGL mesa-libEGL mesa-dri-drivers libX11 \
        cpio e2fsprogs bsdtar flex bison bc xz unzip pipewire-utils pulseaudio-utils alsa-utils
elif command -v pacman >/dev/null; then
    "${elevate[@]}" pacman -S --needed --noconfirm base-devel patch git curl \
        ca-certificates python python-pip ninja pkgconf meson glib2 pixman \
        gtk3 libslirp dtc zlib libffi openssl libepoxy mesa libglvnd libx11 \
        cpio e2fsprogs libarchive flex bison bc xz unzip libpulse alsa-utils
else
    printf 'No supported package manager found. Install the dependencies in README.md and use --no-install.\n' >&2
    exit 1
fi
