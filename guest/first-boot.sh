#!/sbin/busybox sh
exec >>/data/g925-first-boot.log 2>&1
export PATH=/system/bin:/system/xbin:/sbin
echo 'Preparing emulator settings'
get_setting() {
    value=$(/system/bin/sh /system/bin/settings get "$1" "$2" 2>/dev/null)
    # Samsung app_process can append a CSC diagnostic to stdout.
    printf '%s\n' "${value%%[[:space:]]*}"
}
attempt=0
while :; do
    value=$(get_setting global device_provisioned)
    case "$value" in 0|1|null) break ;; esac
    /sbin/busybox sleep 2
    attempt=$((attempt + 1))
    [ "$attempt" -lt 600 ] || exit 1
done
# Reload the media process once the FIFO and host transport are available.
# This also recovers a HAL probe that happened before the init helper ran.
if [ -p /dev/eac ] && [ -n "$(getprop ro.boot.g925audioport)" ]; then
    stop media
    start media
fi
if [ ! -f /data/.g925-initialized ]; then
    first_boot=1
    setprop persist.sys.language en
    setprop persist.sys.country GB
    settings put global device_provisioned 1
    settings put secure user_setup_complete 1
    [ "$(get_setting global device_provisioned)" = 1 ] || exit 1
    [ "$(get_setting secure user_setup_complete)" = 1 ] || exit 1
    settings put system screen_off_timeout 2147483647
    settings put secure lockscreen.disabled 1
    echo 1 >/data/misc/wifi/g925-virtual-network
    /sbin/busybox chmod 0660 /data/misc/wifi/g925-virtual-network
    /sbin/busybox chown 1010:1010 /data/misc/wifi/g925-virtual-network
    for package in com.google.android.setupwizard com.sec.android.app.SecSetupWizard \
        com.sec.android.app.setupwizard com.sec.android.app.factorymode \
        com.samsung.android.securitylogagent; do
        pm disable-user --user 0 "$package" >/dev/null 2>&1
    done
    /sbin/busybox touch /data/.g925-initialized
fi
attempt=0
until [ "$(getprop sys.boot_completed)" = 1 ]; do
    /sbin/busybox sleep 2
    attempt=$((attempt + 1))
    [ "$attempt" -lt 600 ] || exit 1
done
[ "$first_boot" = 1 ] && /system/bin/sh /system/bin/svc wifi enable
echo 'Android boot complete; virtual Wi-Fi enabled'
