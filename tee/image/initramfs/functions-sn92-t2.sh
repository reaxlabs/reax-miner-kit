
# ---- SN92 T2 override (appended by build-on-verda.sh) ----
# Every initramfs failure halts: no shell, no reboot loop, fixed status text only.
panic()
{
	echo "SN92-T2: BOOT HALTED (${1:-error})" >/dev/console 2>/dev/null || echo "SN92-T2: BOOT HALTED"
	while :; do sleep 3600; done
}
