
# ---- SN92 T2 mountroot (appended by build-on-verda.sh; overrides local_mount_root) ----
# All parameters below are compiled into this measured initramfs. root= and every other
# cmdline value are ignored for choosing the root; the OS disk is only a locator.
SN92_VARIANT='@VARIANT@'
SN92_ROOT_HASH='@ROOT_HASH@'
SN92_SALT='@SALT@'
SN92_DATA_BLOCKS='@DATA_BLOCKS@'
SN92_IMG='/sn92-image/@VARIANT@/rootfs.squashfs'
SN92_HASHFILE='/sn92-image/@VARIANT@/rootfs.verity'
SN92_RW_SIZE='@RW_SIZE@'
SN92_LABEL='cloudimg-rootfs'

sn92_mountroot()
{
	B=/run/sn92-t2
	mkdir -p "$B/os" "$B/lower" "$B/rw" /run/cryptsetup || panic "mkdir"
	for m in dm_mod dm_verity overlay squashfs; do modprobe "$m" || panic "modprobe $m"; done
	# Wait up to 60 s for exactly one ext4 filesystem labelled cloudimg-rootfs.
	dev=""; i=0
	while [ $i -lt 120 ]; do
		devs=$(blkid -c /dev/null -o device -t "LABEL=$SN92_LABEL" 2>/dev/null)
		n=$(printf '%s\n' "$devs" | grep -c .)
		if [ "$n" -gt 1 ]; then panic "ambiguous $SN92_LABEL"; fi
		if [ "$n" -eq 1 ]; then dev=$devs; break; fi
		sleep 0.5; i=$((i + 1))
	done
	[ -n "$dev" ] && [ -b "$dev" ] || panic "no $SN92_LABEL"
	[ "$(blkid -c /dev/null -o value -s TYPE "$dev")" = ext4 ] || panic "locator not ext4"
	mount -t ext4 -o ro,noload,nodev,nosuid,noexec "$dev" "$B/os" || panic "mount locator"
	[ -f "$B/os$SN92_IMG" ] && [ -f "$B/os$SN92_HASHFILE" ] || panic "image files missing"
	ld=$(losetup -r -f --show "$B/os$SN92_IMG") || panic "loop data"
	lh=$(losetup -r -f --show "$B/os$SN92_HASHFILE") || panic "loop hash"
	DM_DISABLE_UDEV=1 veritysetup open "$ld" sn92root "$lh" "$SN92_ROOT_HASH" \
		--no-superblock --format=1 --hash=sha256 --data-block-size=4096 --hash-block-size=4096 \
		--data-blocks="$SN92_DATA_BLOCKS" --salt="$SN92_SALT" --panic-on-corruption \
		|| panic "verity open"
	[ -b /dev/mapper/sn92root ] || panic "verity device"
	mount -t squashfs -o ro /dev/mapper/sn92root "$B/lower" || panic "mount verified root"
	mount -t tmpfs -o "size=$SN92_RW_SIZE,mode=0755" sn92-rw "$B/rw" || panic "tmpfs"
	mkdir -p "$B/rw/upper" "$B/rw/work" || panic "rw dirs"
	mount -t overlay -o "lowerdir=$B/lower,upperdir=$B/rw/upper,workdir=$B/rw/work" sn92root "${rootmnt?}" \
		|| panic "overlay"
	[ -x "${rootmnt}/sbin/init" ] || panic "no init"
	echo "SN92-T2: verified root $SN92_VARIANT $(echo "$SN92_ROOT_HASH" | cut -c1-16) mounted" >/dev/console 2>/dev/null
}

local_mount_root()
{
	sn92_mountroot
}

mountroot()
{
	sn92_mountroot
}
