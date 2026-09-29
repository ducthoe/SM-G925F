#!/sbin/busybox sh
exec >>/data/g925-audio-relay.log 2>&1
export PATH=/system/bin:/system/xbin:/sbin
port=$(getprop ro.boot.g925audioport)
case "$port" in ''|*[!0-9]*) exit 0 ;; esac
[ -p /dev/eac ] || /sbin/busybox mkfifo -m 660 /dev/eac
/sbin/busybox chown 0:1005 /dev/eac
restorecon /dev/eac
ip link set wlan0 up
ip addr add 10.0.2.15/24 dev wlan0 2>/dev/null
ip route add 10.0.2.2/32 dev wlan0 2>/dev/null
exec /sbin/audio-relay "$port"
