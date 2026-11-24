# Verda SNP guest image: design, not release-ready

## Live T2 RELEASE result (24 Nov 2026, pod f5f58d28, slice t2release)

Both variants built on the stock boot (`build debug`, `build release`, ~80 s + ~25 s). DEBUG boot: SSH back after
91 s (the `ssh.service` drop-in fix works; no ordering cycle), dead-man disarmed, `/` = overlay on the dm-verity
squashfs, SNP MEASUREMENT == prediction `cd47be5e…5b5d`, GPU CC ready, agent healthy, podman runs the preloaded image.
`install release` from the DEBUG boot, reboot: agent healthz from our verifier host after 83 s, MEASUREMENT == prediction
`f7375d2b…e436`, TCP 22 refused, verifier with `policy/t2-verda-rtxpro6000-cc-2404-release-f7375d2bf393.json`
(T2 list = release digest, `required_assurance: T2`): ok=true, assurance T2. Details: `MANIFEST-2026-11-24-t2release.json`.
Operational note: the agent admits one `/attest` per source IP per 10 s; an unverified probe call (to read
`wg_pubkey`) must be followed by a pause of >10 s before the verifier call, or the verifier records `challenge`.

## What actually worked (live, 24 Nov 2026, VM tee-t2-build-4)

`image/build-on-verda.sh` builds the image on a T1-provisioned Verda `1RTXPRO6000.30V.CC` VM
(`provision_node.sh` path, then `sn92-enable`). Live result for the DEBUG variant: the SNP report MEASUREMENT after
reboot equalled the prediction (`567343b8...`), and the repo verifier with a T2 policy (the T1 policy plus
`assurance_level: T2` and that digest) returned ok=True with no failed checks (SNP, NRAS GB20X, binding, RTT/ASN).
The agent answered `/healthz` from our verifier host, and the per-boot WireGuard key was new.
Record: `image/MANIFEST-2026-11-24.json`. Before the change, the stock prediction == report == `f82b40...` on this
instance, the same value as on probe 1's instance. So the measured cmdline (`root=UUID=59134bd0-...`) is the same
across instances of this image (2 instances seen).

How it works (all in the script, ~2 min on 30 vCPUs):
1. **Snapshot**: non-recursive read-only bind of `/` (raw ext4, no /proc,/sys,/dev,/run,/boot mounts), then an
   overlay with a tmpfs upper for the variant edits. Excluded: /tmp, /var/tmp, apt lists/cache, /root/.cache, SSH
   host keys, journal files, podman libpod db, the build and image dirs. Common edits: cloud-init disabled
   (operator user-data would otherwise run as root), fstab without `/`, podman `storage.conf`, core dumps off.
2. **squashfs** (zstd-3, `SOURCE_DATE_EPOCH` clamps timestamps; 6.5 GB) + **dm-verity** tree in a separate file,
   `--no-superblock`, sha256, 4K/4K, fixed salt `sha256("sn92-t2-verity-salt-v1")`. Stored as
   `/sn92-image/<variant>/rootfs.{squashfs,verity}` on the cloudimg ext4.
3. **initramfs**: `mkinitramfs -d <copy of /etc/initramfs-tools>` (the host's /etc is not modified). Added: a hook
   (veritysetup, losetup, blkid, dm-verity/overlay modules), plus overrides appended to `scripts/local` and
   `scripts/functions`. The new `mountroot` ignores `root=`. It finds exactly one ext4 `LABEL=cloudimg-rootfs`,
   mounts it `ro,noload,noexec`, attaches both files to read-only loop devices and runs
   `veritysetup open --no-superblock` with root hash, salt, block sizes and data-block count compiled in, plus
   `--panic-on-corruption`. It then mounts the verified squashfs, overlays a 16G tmpfs and switches root.
   `panic()` is overridden: any failure prints `SN92-T2: BOOT HALTED (...)` and sleeps forever (no shell). The mounts
   live under `/run/sn92-t2/{os,lower,rw}` and move into the real root with /run.
4. **Install**: kernel unchanged. The stock initrd is copied to `/boot/initrd.img-6.8.0-149-generic.stock` (and the
   kernel to `vmlinuz-...stock`). The new initrd replaces `/boot/initrd.img-6.8.0-149-generic` in place via
   temp-file + rename + sync (the path update-initramfs uses and probe 1 showed Verda picks up on reboot).
5. **Measure**: `sev-snp-measure==0.0.13 --mode snp --vcpus 30 --vcpu-type EPYC-Turin` on /boot/OVMF.amdsev.fd, the
   kernel, the new initrd and `/proc/cmdline` minus `initrd=initrd `. `build-on-verda.sh report` reads a fresh
   MEASUREMENT via configfs-tsm for comparison.

Podman on an overlay root: the overlay driver refuses an overlayfs graphroot. So graphroot is a fresh tmpfs (fstab).
Preloaded images come read-only from the verified squashfs through
`additionalimagestores = ["/run/sn92-t2/lower/var/lib/containers/storage"]`. **Not yet observed live** (no SSH).
The verifier result and agent prove the boot chain started. That `sn92-gpu-check` reached ready is not shown
separately.

Variants: DEBUG keeps sshd (fresh host keys per boot via an `ssh.service` drop-in that runs `ssh-keygen -A`), root's
authorized key, `/boot` mounted, and a **dead-man timer**. Twenty-five minutes after boot it restores the stock
initrd and reboots unless disarmed over SSH (`systemctl stop sn92-debug-deadman.timer`). RELEASE drops all SSH keys,
masks ssh/getty/serial-getty/console-getty/emergency/rescue/debug-shell, podman-auto-update and apt timers, and does
not mount /boot. It sets `kernel.sysrq=0`, quiet printk and `ShowStatus=no`, and prints one fixed line to ttyS0. Its
WireGuard key comes from the bundle: `tools/t2_attest_probe.py <ip> <hotkey>` makes one unverified `/attest` call
and prints `wg_pubkey` and MEASUREMENT. Pin that key in nodes.json, then run the verifier. No agent change was
needed.

Live failures and fixes:
- The first DEBUG build used a separate host-key unit with `Before=ssh.socket`. That is an ordering cycle
  (ssh.socket -> sockets.target -> basic.target -> unit), so systemd dropped ssh.socket and port 22 was refused.
  The box stayed attested but could not be administered (as designed: nothing else gets in). The fix is the drop-in,
  plus a build-time `systemd-analyze verify --root` cycle check and the dead-man timer. **Fix not yet live-tested.**
- `mksquashfs` drops POSIX ACL xattrs (`system.posix_acl_*`, e.g. on /var/log/journal). Harmless here.
- `mksquashfs` refuses `SOURCE_DATE_EPOCH` together with `-mkfs-time`.

Still missing for a releasable T2: the rootfs is a snapshot of a live, apt-provisioned system (not bit-reproducible,
no package snapshot); the serving image is a CUDA base placeholder; the nftables egress policy is not enabled in the
image; and the RELEASE variant has not been booted. The per-instance `/sn92-image` files and `/boot` initrd can be
replaced by the provider or operator only to deny service: any change alters the launch digest or halts the boot.

## Original design notes

## Trust and boot

Verda direct-boots OVMF, Ubuntu `6.8.0-149-generic` and an initramfs from
`/boot`. The provider supplies `console=ttyS0 root=UUID=<cloudimg uuid> ro
net.ifnames=0 biosdevname=0 fsck.mode=auto fsck.repair=yes pci=realloc,nocrs`.
The initramfs must ignore `root=` for choosing the *guest root*. It must find
one uniquely identified OS filesystem read-only, open its two fixed-path image
files read-only through loop devices, and create a dm-verity device using a
root hash embedded in the measured initramfs. It mounts the verified EROFS
read-only and switches root. A missing, ambiguous, malformed or failed device,
verity check, mount or switch-root halts boot without a shell. dm-verity must
use a fixed, documented format, salt, block size and data length, also embedded
in the initramfs; do not accept those values from the disk or command line.
The OS filesystem is untrusted even if identified by the cmdline UUID: it
is a locator only. Every read of guest root files must traverse dm-verity.
Do not trust host-provided modules, configuration, container layers, mutable
bind mounts or executables. The root hash transitively commits rootfs contents;
SNP measurement commits OVMF, exact kernel, initramfs and exact cmdline.
The verifier must allow-list the actual predicted digest and require debug off,
non-migratable policy and the approved TCB; a successful boot alone proves
nothing to a remote client. Rollback is blocked by removing old measurements
from verifier policy, not by the installer. The kernel and NVIDIA modules must
be the matching 6.8.0-149 build, with pinned module signing and Secure Boot
assumptions verified on the target.

## Runtime

The only durable OS-disk files are the verified image, its verity tree and boot
artifacts. Mount `/run`, `/tmp`, `/var/log` and container runtime state on
bounded tmpfs; disable swap and core dumps. For large scratch, use a dedicated
non-root device or bounded ephemeral file and dm-crypt with a fresh random key
read directly from `/dev/urandom` each boot. Never log, persist, or expose the
key; fail closed on absent/ambiguous scratch devices. Encrypt before writing
customer data; a reboot loses the key. Explicitly separate persistent podman
image storage, preloaded into the verified rootfs by immutable OCI digest,
from ephemeral writable container state. No image pulls at runtime. Launch only
the measured digest, with no privileged container, host mount or runtime
configuration controlled by the operator. The agent and its dependencies are
likewise included in the rootfs; its workload JSON must contain the exact
serving image digest and versions matching `INTERFACES.md`. Start serving only
after the required GPU and policy checks, and do not expose an administrative
shell. Disable sshd, gettys, emergency shells and debug access; ttyS0 prints
only fixed status text, with no login input. Avoid sensitive data in logs.

Generate a WireGuard private key in RAM on each boot and expose only its public
key to the agent. Provision peer configuration through an authenticated,
attested channel; the gateway must pin this fresh public key as described in
`INTERFACES.md`. Apply a measured nftables policy before networking or starting
services, default-deny inbound and outbound, with explicit gateway, NRAS,
collateral and registry destinations as needed. Names resolved over DNS are
not a stable allow-list; pinned IPs or an authenticated update protocol and
rules for DNS, DHCP, IPv6 and WireGuard are needed. A missing policy must stop
network services. The agent's HTTPS port 8443 and inference over WireGuard
port 8080 are separate endpoints. Hardware, routing, NVIDIA 595 CDI and GPU
readiness must be validated on the actual VM.

## Building and installation

A release build requires an immutable Ubuntu package snapshot and exact package
versions and hashes, fixed source epoch, deterministic filesystem and initramfs
metadata, fixed verity salt and reproducible toolchain, pinned base OCI digest
and complete image data, and the exact matching kernel/modules. Record all
inputs and artifact SHA-256s; use `sev-snp-measure --mode snp --vcpus 30
--vcpu-type EPYC-Turin` with the exact OVMF, kernel, initrd and provider cmdline
including that instance's UUID. Independently rebuild and compare bytes and
compare predicted digest to a real SNP report before adding it to the golden
policy. Installer must verify every artifact and target identity, refuse an
existing installation or insufficient space, preserve and fsync stock boot
artifacts, atomically replace boot files and sync before reboot. Console
rollback is provider-controlled and has not been demonstrated. Changing the
OS disk or boot files can deny service but cannot authenticate altered code.

This repository does not yet include a package snapshot/lock, serving OCI image or
an image-enforced network policy. `build.sh`/`install-on-verda.sh` therefore still refuse a
production release; `build-on-verda.sh` builds the live-tested test images described above. A test image must not be
accepted as production T2.

## Related systems

[Chutes sek8s](https://www.chutes.ai/tee) uses Intel TDX guests and signed
container admission, with validator checks of CPU and NVIDIA evidence. It uses
a Kubernetes-oriented stack; this proposal uses a smaller fixed Podman workload
and Verda SNP measurement. The security properties of either must be verified
against its own exact release and policy.

[Phala dstack](https://github.com/Phala-Network/dstack-cloud/blob/master/docs/security/security-model.md)
measures OS and application identity, including a compose hash, and uses an
attested KMS for application keys. The proposed MVP instead embeds the fixed
rootfs hash in the measured initramfs and uses per-boot scratch keys; it does
not yet implement dstack's attested KMS or application provisioning.
