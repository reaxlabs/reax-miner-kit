#!/usr/bin/env bash
# SN92 T2 measured image for Verda SEV-SNP CVMs (24.04.cuda13.4.cc, kernel 6.8.0-149-generic).
# Runs ON a freshly provisioned (T1 path) Verda VM as root. See image/DESIGN.md.
#
#   build-on-verda.sh build   <debug|release>  rootfs squashfs + dm-verity tree + initramfs with the root hash compiled
#                                               in + predicted SNP launch digest -> /sn92-image/<v>/ (+ MANIFEST.json)
#   build-on-verda.sh install <debug|release>  put that initramfs into /boot (stock kept as *.stock), fsync; also works
#                                               from a DEBUG boot (build both variants on the stock boot first)
#   build-on-verda.sh restore                  put the stock initramfs back (debug/rollback)
#   build-on-verda.sh measure <file-initrd>    predict the launch digest for this box's OVMF/kernel/cmdline
#   build-on-verda.sh report                   MEASUREMENT of a fresh SNP report (configfs-tsm)
#
# The OS disk (ext4 "cloudimg-rootfs") is only a locator for the two image files; the initramfs (measured) holds the
# verity root hash and parameters, so every byte of the running root is committed by the SNP launch digest.
set -euo pipefail
umask 022
KVER=6.8.0-149-generic
EPOCH=${SN92_SOURCE_DATE_EPOCH:-1763942400}          # fixed: 2025-11-24T00:00:00Z
SALT=$(printf '%s' 'sn92-t2-verity-salt-v1' | sha256sum | cut -d' ' -f1)
RW_SIZE=${SN92_RW_SIZE:-16g}
CONTAINERS_TMPFS=${SN92_CONTAINERS_TMPFS:-32g}
SNPM_VERSION=0.0.13
HERE=$(cd "$(dirname "$0")" && pwd)
die() { echo "build-on-verda: $*" >&2; exit 1; }
[[ $EUID == 0 ]] || die "run as root"
[[ $(uname -r) == "$KVER" ]] || die "expected running kernel $KVER"

cmdline_append() {  # provider cmdline as OVMF passes it, minus the 'initrd=initrd ' OVMF adds itself
  local c; c=$(cat /proc/cmdline); c=${c#initrd=initrd }; printf '%s' "$c"
}

snpm() {
  local venv=/sn92-t2/venv
  if [[ ! -x $venv/bin/sev-snp-measure ]]; then
    python3 -m venv "$venv" >/dev/null && "$venv/bin/pip" install -q "sev-snp-measure==$SNPM_VERSION" >/dev/null
  fi
  "$venv/bin/sev-snp-measure" "$@"
}

measure() {  # $1 = initrd path
  snpm --mode snp --vcpus 30 --vcpu-type EPYC-Turin --ovmf /boot/OVMF.amdsev.fd --kernel "/boot/vmlinuz-$KVER" \
    --initrd "$1" --append "$(cmdline_append)" --output-format hex
}

report() {  # fresh SNP report via configfs-tsm; prints the 48-byte MEASUREMENT (offset 0x90) as hex
  python3 -I - <<'PY'
import os, secrets, tempfile
base = '/sys/kernel/config/tsm/report'
d = tempfile.mkdtemp(prefix='sn92m', dir=base)
try:
    with open(d + '/inblob', 'wb') as f: f.write(secrets.token_bytes(64))
    with open(d + '/outblob', 'rb') as f: r = f.read()
    print(r[0x90:0x90 + 48].hex())
finally:
    os.rmdir(d)
PY
}

variant_customize() {  # $1 = variant, $2 = merged root to edit
  local v=$1 M=$2
  # common: no cloud-init (operator user-data would be root code execution), no persistent host keys
  touch "$M/etc/cloud/cloud-init.disabled"
  rm -f "$M"/etc/ssh/ssh_host_*
  install -m 0644 "$HERE/rootfs/storage.conf" "$M/etc/containers/storage.conf"
  install -m 0644 "$HERE/rootfs/90-sn92-t2.conf" "$M/etc/sysctl.d/90-sn92-t2.conf"
  install -d -m 0700 "$M/var/lib/containers/storage"
  {
    echo '# SN92 T2: root is the dm-verity squashfs + tmpfs overlay set up by the measured initramfs.'
    [[ $v == debug ]] && echo 'LABEL=BOOT /boot ext4 defaults,nofail 0 2'
    echo "tmpfs /var/lib/containers/storage tmpfs mode=0700,size=$CONTAINERS_TMPFS 0 0"
  } > "$M/etc/fstab"
  printf 'variant=%s\nkernel=%s\nbuilt_epoch=%s\n' "$v" "$KVER" "$EPOCH" > "$M/etc/sn92-t2-variant"
  if [[ $v == debug ]]; then
    grep -q '^ExecStartPre=/usr/sbin/sshd -t$' "$M/lib/systemd/system/ssh.service" || die "unexpected ssh.service"
    install -d "$M/etc/systemd/system/ssh.service.d"
    install -m 0644 "$HERE/rootfs/ssh-hostkeys.conf" "$M/etc/systemd/system/ssh.service.d/10-sn92-hostkeys.conf"
    [[ -L $M/etc/systemd/system/sockets.target.wants/ssh.socket ]] || die "ssh.socket not enabled in debug image"
    install -m 0644 "$HERE/rootfs/sn92-debug-deadman.service" "$HERE/rootfs/sn92-debug-deadman.timer" "$M/etc/systemd/system/"
    systemctl --root="$M" enable sn92-debug-deadman.timer >/dev/null 2>&1
    [[ -L $M/etc/systemd/system/timers.target.wants/sn92-debug-deadman.timer ]] || die "dead-man timer not enabled"
  else
    rm -rf "$M/root/.ssh" "$M"/home/*/.ssh
    install -m 0644 "$HERE/rootfs/91-sn92-t2-release.conf" "$M/etc/sysctl.d/91-sn92-t2-release.conf"
    install -m 0644 "$HERE/rootfs/sn92-console-status.service" "$M/etc/systemd/system/sn92-console-status.service"
    systemctl --root="$M" enable sn92-console-status.service >/dev/null 2>&1
    install -d "$M/etc/systemd/system.conf.d"
    printf '[Manager]\nShowStatus=no\n' > "$M/etc/systemd/system.conf.d/90-sn92-t2.conf"
    systemctl --root="$M" mask ssh.service ssh.socket getty@.service serial-getty@.service console-getty.service \
      container-getty@.service autovt@.service emergency.service emergency.target rescue.service rescue.target \
      debug-shell.service podman-auto-update.service podman-auto-update.timer unattended-upgrades.service \
      apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1
    for u in ssh.service ssh.socket getty@.service serial-getty@.service console-getty.service emergency.service \
             rescue.service debug-shell.service; do
      [[ $(readlink "$M/etc/systemd/system/$u") == /dev/null ]] || die "$u not masked"
    done
    [[ ! -e $M/root/.ssh && -z $(ls "$M"/home/*/.ssh 2>/dev/null) ]] || die "ssh keys left in release"
  fi
}

check_units() {  # fail on ordering cycles among the units we add or rely on (systemd 255 verify --root; untested live)
  local M=$1 out
  out=$(systemd-analyze verify --root="$M" --man=no default.target sn92-attest.service wg-quick@wg0.service \
        sn92-gpu-check.service ssh.socket 2>&1 || true)
  if grep -Eiq 'ordering cycle|job .* deleted' <<<"$out"; then
    printf '%s\n' "$out" >&2; die "systemd ordering cycle in image"
  fi
}

build() {
  local v=$1 O=/sn92-image/$1
  W=/sn92-t2/$1   # global: used by the EXIT trap
  [[ $(blkid -c /dev/null -o device -t LABEL=cloudimg-rootfs | wc -l) == 1 ]] || die "need exactly one cloudimg-rootfs"
  [[ $(findmnt -no SOURCE /) == "$(blkid -c /dev/null -o device -t LABEL=cloudimg-rootfs)" ]] || die "/ is not cloudimg-rootfs (already a T2 boot?)"
  command -v mksquashfs veritysetup mkinitramfs >/dev/null || die "need squashfs-tools cryptsetup-bin initramfs-tools"
  systemctl stop sn92-serving.service 2>/dev/null || true
  mkdir -p "$W/snap" "$W/tmp" "$W/merged" "$O"
  cleanup() { umount "$W/merged" 2>/dev/null || true; umount "$W/tmp" 2>/dev/null || true; umount "$W/snap" 2>/dev/null || true; }
  trap cleanup EXIT
  cleanup
  sync
  # Non-recursive bind = the raw OS filesystem without /proc,/sys,/dev,/run,/boot mounts; made read-only.
  mount --bind / "$W/snap"; mount -o remount,bind,ro "$W/snap"
  mount -t tmpfs -o size=2g sn92-build "$W/tmp"; mkdir -p "$W/tmp/upper" "$W/tmp/work"
  mount -t overlay -o "lowerdir=$W/snap,upperdir=$W/tmp/upper,workdir=$W/tmp/work" sn92-build "$W/merged"
  variant_customize "$v" "$W/merged"
  check_units "$W/merged"
  local ex=(tmp/'*' var/tmp/'*' var/lib/apt/lists/'*' var/cache/apt/'*' root/.cache sn92-t2 sn92-image
            'etc/ssh/ssh_host_*' var/log/journal/'*' lost+found swap.img var/lib/containers/storage/libpod)
  rm -f "$O/rootfs.squashfs" "$O/rootfs.verity"
  SOURCE_DATE_EPOCH=$EPOCH mksquashfs "$W/merged" "$O/rootfs.squashfs" -noappend -quiet -no-progress -comp zstd \
    -Xcompression-level 3 -processors "$(nproc)" -wildcards -e "${ex[@]}"
  cleanup; trap - EXIT
  local size; size=$(stat -c %s "$O/rootfs.squashfs"); (( size % 4096 == 0 )) || die "squashfs not 4K aligned"
  local blocks=$((size / 4096))
  local vout; vout=$(veritysetup format --no-superblock --format=1 --hash=sha256 --data-block-size=4096 \
    --hash-block-size=4096 --data-blocks="$blocks" --salt="$SALT" "$O/rootfs.squashfs" "$O/rootfs.verity")
  local root_hash; root_hash=$(awk '/^Root hash:/ {print $3}' <<<"$vout")
  [[ $root_hash =~ ^[0-9a-f]{64}$ ]] || die "no root hash"
  veritysetup verify --no-superblock --format=1 --hash=sha256 --data-block-size=4096 --hash-block-size=4096 \
    --data-blocks="$blocks" --salt="$SALT" "$O/rootfs.squashfs" "$O/rootfs.verity" "$root_hash" || die "verity verify"

  # initramfs: copy of the stock config + our hook; scripts/local and scripts/functions get the T2 overrides appended
  local C=$W/initramfs-conf; rm -rf "$C"; cp -a /etc/initramfs-tools "$C"
  install -m 0755 "$HERE/initramfs/hook-sn92-t2" "$C/hooks/sn92-t2"
  cp /usr/share/initramfs-tools/scripts/local "$C/scripts/local"
  sed -e "s/@VARIANT@/$v/g" -e "s/@ROOT_HASH@/$root_hash/" -e "s/@SALT@/$SALT/" -e "s/@DATA_BLOCKS@/$blocks/" \
      -e "s/@RW_SIZE@/$RW_SIZE/" "$HERE/initramfs/local-sn92-t2.sh" >> "$C/scripts/local"
  cp /usr/share/initramfs-tools/scripts/functions "$C/scripts/functions"
  cat "$HERE/initramfs/functions-sn92-t2.sh" >> "$C/scripts/functions"
  grep -q '@' <(grep '^SN92_' "$C/scripts/local") && die "unfilled placeholder"
  SOURCE_DATE_EPOCH=$EPOCH mkinitramfs -d "$C" -o "$O/initrd.img" "$KVER"
  local lst; lst=$(lsinitramfs "$O/initrd.img")   # no pipe into grep -q: SIGPIPE + pipefail
  for f in sbin/veritysetup sbin/losetup sbin/blkid drivers/md/dm-verity.ko fs/overlayfs/overlay.ko; do
    grep -qF "$f" <<<"$lst" || die "$f missing in initramfs"
  done

  local digest; digest=$(measure "$O/initrd.img")
  [[ $digest =~ ^[0-9a-f]{96}$ ]] || die "measure failed"
  write_manifest "$v" "$O" "$root_hash" "$blocks" "$digest"
  echo "variant=$v root_hash=$root_hash data_blocks=$blocks predicted_launch_digest=$digest"
}

write_manifest() {
  local v=$1 O=$2 rh=$3 blocks=$4 digest=$5
  SN92_V=$v SN92_O=$O SN92_RH=$rh SN92_BL=$blocks SN92_DG=$digest SN92_SALT=$SALT SN92_EP=$EPOCH \
  SN92_CMD="$(cmdline_append)" SN92_KVER=$KVER SN92_RW=$RW_SIZE SN92_SNPMV=$SNPM_VERSION python3 -I - <<'PY'
import hashlib, json, os, subprocess
e = os.environ
def sha(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''): h.update(b)
    return h.hexdigest()
def out(*a):
    try: return subprocess.run(a, capture_output=True, text=True, timeout=60).stdout.strip()
    except Exception: return None
O = e['SN92_O']
dpkg = out('dpkg-query', '-W', '-f', '${Package}=${Version}\n')
m = {
  'schema': 'sn92-t2-manifest-v1', 'variant': e['SN92_V'], 'platform': 'Verda 1RTXPRO6000.30V.CC, image 24.04.cuda13.4.cc',
  'kernel_release': e['SN92_KVER'], 'source_date_epoch': int(e['SN92_EP']),
  'verity': {'format': 1, 'hash': 'sha256', 'data_block_size': 4096, 'hash_block_size': 4096, 'superblock': False,
             'data_blocks': int(e['SN92_BL']), 'salt': e['SN92_SALT'], 'salt_derivation': 'sha256("sn92-t2-verity-salt-v1")',
             'root_hash': e['SN92_RH']},
  'tmpfs_overlay_size': e['SN92_RW'],
  'cmdline_append': e['SN92_CMD'],
  'measure': {'tool': 'sev-snp-measure==' + e['SN92_SNPMV'], 'mode': 'snp', 'vcpus': 30, 'vcpu_type': 'EPYC-Turin',
              'predicted_launch_digest': e['SN92_DG']},
  'artifacts_sha256': {
    'ovmf:/boot/OVMF.amdsev.fd': sha('/boot/OVMF.amdsev.fd'),
    'kernel:/boot/vmlinuz-' + e['SN92_KVER']: sha('/boot/vmlinuz-' + e['SN92_KVER']),
    'initrd.img': sha(O + '/initrd.img'),
    'rootfs.squashfs': sha(O + '/rootfs.squashfs'),
    'rootfs.verity': sha(O + '/rootfs.verity'),
  },
  'tools': {'mksquashfs': (out('mksquashfs', '-version') or '').splitlines()[0:1],
            'veritysetup': out('veritysetup', '--version'),
            'initramfs-tools': out('dpkg-query', '-W', '-f', '${Version}', 'initramfs-tools')},
  'dpkg_selections_sha256': hashlib.sha256(dpkg.encode()).hexdigest() if dpkg else None,
  'dpkg_package_count': len(dpkg.splitlines()) if dpkg else None,
}
with open(O + '/MANIFEST.json', 'w') as f: json.dump(m, f, indent=1, sort_keys=True); f.write('\n')
with open(O + '/dpkg-selections.txt', 'w') as f: f.write((dpkg or '') + '\n')
PY
}

install_variant() {
  local v=$1 O=/sn92-image/$1 B=/boot/initrd.img-$KVER
  # from inside a T2 DEBUG boot the OS disk (with /sn92-image) is mounted read-only at /run/sn92-t2/os
  [[ -d /run/sn92-t2/os/sn92-image/$v ]] && O=/run/sn92-t2/os/sn92-image/$v
  [[ -f $O/MANIFEST.json && -f $O/initrd.img && -f $O/rootfs.squashfs && -f $O/rootfs.verity ]] || die "build $v first"
  mountpoint -q /boot || die "/boot not mounted"
  python3 -I - "$O" <<'PY' || die "artifact hash mismatch"
import hashlib, json, sys
O = sys.argv[1]; m = json.load(open(O + '/MANIFEST.json'))
for k in ('initrd.img', 'rootfs.squashfs', 'rootfs.verity'):
    h = hashlib.sha256()
    with open(O + '/' + k, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''): h.update(b)
    assert h.hexdigest() == m['artifacts_sha256'][k], k
PY
  [[ -f $B.stock ]] || cp -a --remove-destination "$(readlink -f "$B")" "$B.stock"
  [[ -f /boot/vmlinuz-$KVER.stock ]] || cp -a "/boot/vmlinuz-$KVER" "/boot/vmlinuz-$KVER.stock"
  cp "$O/initrd.img" "$B.sn92-new"; sync -f "$B.sn92-new"
  mv -f "$B.sn92-new" "$B"; sync
  echo "installed $v initramfs as $B (stock: $B.stock); predicted digest $(python3 -I -c 'import json,sys;print(json.load(open(sys.argv[1]))["measure"]["predicted_launch_digest"])' "$O/MANIFEST.json")"
}

restore() {
  local B=/boot/initrd.img-$KVER
  [[ -f $B.stock ]] || die "no stock copy"
  cp "$B.stock" "$B.sn92-new"; sync; mv -f "$B.sn92-new" "$B"; sync; echo "stock initramfs restored"
}

case ${1:-} in
  build) [[ ${2:-} == debug || ${2:-} == release ]] || die "variant debug|release"; build "$2" ;;
  install) [[ ${2:-} == debug || ${2:-} == release ]] || die "variant debug|release"; install_variant "$2" ;;
  restore) restore ;;
  measure) [[ -f ${2:-} ]] || die "initrd path"; measure "$2" ;;
  report) report ;;
  *) sed -n '2,12p' "$0"; exit 2 ;;
esac
