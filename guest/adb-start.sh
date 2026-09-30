#!/sbin/busybox sh
set -eu
exec >>/data/g925-adb.log 2>&1
export PATH=/system/bin:/system/xbin:/sbin

# Android's Wi-Fi services manage wlan0. Keep debugging on its own link,
# with no default route or DNS changes, even when Wi-Fi is turned off.
ip addr replace 10.0.3.15/24 dev adb0
ip link set adb0 up
setprop service.adb.tcp.port 5555
start adbd
echo 'ADB ready on the isolated virtual link'
