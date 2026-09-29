# Source and license notices

This project packages the existing S6 Edge emulator integration. It does not include Samsung firmware, Google binary packages, generated phone images, or toolchains in its source repository.

- The QEMU integration and patches derive from QEMU, licensed under GPL version 2 and compatible licenses. The downloaded QEMU tree includes its complete license notices.
- The Hacker Kernel and the kernel patch/module derive from Linux and are licensed under GPL version 2. The pinned source comes from `HRTKernel/Hacker_Kernel_SM-G92X_old`; copied virtio input code derives from Linux 4.4.
- The Android graphics headers and virtual supplicant use Apache License 2.0. The headers preserve their upstream copyright notices. `PixelFormat.h` comes from Android/LineageOS framework headers; private Bionic headers are from Android 5.1.
- The compatible guest graphics build uses the pinned LineageOS goldfish source and AOSP system/core and hardware/libhardware headers. Their source trees include applicable license notices.
- Android NDK and legacy SDK binary downloads retain Google's applicable licenses. Their archives are fetched locally from Google, and redistributed source should not include the download cache. The API 21 guest library binaries are extracted from the original emulator system package.
- The static BusyBox binary is obtained from Debian; BusyBox is licensed under GPL version 2. Corresponding Debian source is available through the Debian `busybox` source package.
- SuperSU 2.49 and its ARM64 tools are obtained from the pinned Hacker Kernel bundle and retain their upstream licensing. They are downloaded locally with the kernel source; this checkout does not vendor their binaries. Integration follows [Chainfire's embedding documentation](https://su.chainfire.eu/#embedding).

Pinned revisions, download locations, and binary hashes are recorded in `versions.json`. Preserve upstream notices when redistributing changes. Firmware remains a local user input.
