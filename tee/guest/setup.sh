#!/usr/bin/env bash
set -euo pipefail

# Prototype installer. Service startup remains gated on reviewed W2 and configuration.
DRY=${SN92_DRY_RUN:-0}
run() { if [[ $DRY == 1 ]]; then printf '+'; printf ' %q' "$@"; printf '\n'; else "$@"; fi; }
if [[ $DRY != 0 && $DRY != 1 ]]; then echo 'SN92_DRY_RUN must be 0 or 1' >&2; exit 1; fi
if [[ $DRY == 0 && $EUID != 0 ]]; then echo 'Run as root' >&2; exit 1; fi
# Explicit pinned versions prevent a silent moving target. Supply Ubuntu archive version for the chosen release.
: "${SN92_PODMAN_VERSION:=4.9.3}"
: "${SN92_NVATTEST_VERSION:=2.0.0}"
: "${SN92_DRIVER_VERSION:=595.91.07}"
: "${SN92_ENABLE_EGRESS_LOCK:=0}"
[[ $SN92_ENABLE_EGRESS_LOCK == 0 || $SN92_ENABLE_EGRESS_LOCK == 1 ]] || { echo "Invalid egress switch" >&2; exit 1; }
: "${SN92_TOOLKIT_VERSION:=1.20.2-1}"
: "${SN92_NVIDIA_KEY_FINGERPRINT:?Set independently verified NVIDIA apt signing key fingerprint}"
[[ $SN92_NVIDIA_KEY_FINGERPRINT =~ ^[[:xdigit:]]{40}$ ]] || { echo 'Expected 40 hex fingerprint' >&2; exit 1; }
if [[ $DRY == 0 ]]; then
  # shellcheck source=/dev/null
  source /etc/os-release
  [[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 24.04 ]] || { echo 'Unsupported OS' >&2; exit 1; }
  if ! command -v lspci >/dev/null || ! lspci -nn | grep -qi '10de:2bb5'; then echo 'Expected GPU not found' >&2; exit 1; fi
  [[ -d /sys/kernel/config/tsm/report ]] || { echo 'TSM report unavailable' >&2; exit 1; }
  [[ $(nvidia-smi --query-gpu=driver_version --format=csv,noheader | sort -u) == "$SN92_DRIVER_VERSION" ]] || { echo 'Unexpected driver version' >&2; exit 1; }
  nvidia-smi conf-compute -f | grep -Eq 'CC status:[[:space:]]*ON' || { echo 'CC not ON' >&2; exit 1; }
fi
run apt-get update
run apt-get install -y ca-certificates curl gnupg pciutils wireguard-tools nftables python3
# Apt package revisions vary; choose only a package whose upstream version matches the pin.
resolve_version() {
  local package=$1 version=$2 candidate
  candidate=$(apt-cache madison "$package" | awk -F '|' -v v="$version" '{gsub(/^[[:space:]]+|[[:space:]]+$/, "", $2); if (index($2, v) == 1 && (length($2) == length(v) || substr($2, length(v)+1, 1) ~ /[+~.-]/)) {print $2; exit}}')
  [[ -n $candidate ]] || { echo "Pinned package unavailable: $package" >&2; return 1; }
  printf '%s' "$candidate"
}
if [[ $DRY == 1 ]]; then
  echo "+ resolve and install podman=$SN92_PODMAN_VERSION and nvattest=$SN92_NVATTEST_VERSION"
else
  run apt-get install -y "podman=$(resolve_version podman "$SN92_PODMAN_VERSION")" "nvattest=$(resolve_version nvattest "$SN92_NVATTEST_VERSION")"
fi
# Do not trust the downloaded key until its full fingerprint matches an independent source.
if [[ $DRY == 1 ]]; then
  echo '+ download NVIDIA apt signing key and verify full fingerprint before installing repository'
else
  tmp_key=$(mktemp /run/sn92-nvidia-key.XXXXXX)
  trap 'rm -f "$tmp_key"' EXIT
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey -o "$tmp_key"
  fingerprint=$(gpg --show-keys --with-colons "$tmp_key" | awk -F: '$1 == "fpr" {print $10; exit}')
  [[ ${fingerprint^^} == "${SN92_NVIDIA_KEY_FINGERPRINT^^}" ]] || { echo 'NVIDIA key fingerprint mismatch' >&2; exit 1; }
  gpg --dearmor < "$tmp_key" > /usr/share/keyrings/sn92-nvidia-container.gpg
  # shellcheck disable=SC2016 # apt expands its literal $(ARCH) placeholder.
  printf '%s\n' 'deb [signed-by=/usr/share/keyrings/sn92-nvidia-container.gpg] https://nvidia.github.io/libnvidia-container/stable/deb/$(ARCH) /' > /etc/apt/sources.list.d/sn92-nvidia-container.list
fi
run apt-get update
run apt-get install -y "nvidia-container-toolkit=$SN92_TOOLKIT_VERSION" "nvidia-container-toolkit-base=$SN92_TOOLKIT_VERSION" "libnvidia-container-tools=$SN92_TOOLKIT_VERSION" "libnvidia-container1=$SN92_TOOLKIT_VERSION"
run install -d -m 0755 /etc/cdi
run systemctl mask --now nvidia-cdi-refresh.service nvidia-cdi-refresh.path
run rm -f /var/run/cdi/nvidia.yaml
if [[ $DRY == 1 ]]; then
  echo '+ generate CDI and rewrite as 0.6.0 without additionalGids in /etc/cdi/nvidia.yaml'
else
  cdi_tmp=$(mktemp --suffix=.yaml /etc/cdi/.nvidia.XXXXXX)  # nvidia-ctk appends .yaml to extension-less paths (live 24 Nov)
  nvidia-ctk cdi generate --format=yaml --output="$cdi_tmp"
  python3 - "$cdi_tmp" <<'PYCDI'
import re
import sys
from pathlib import Path
path = Path(sys.argv[1])
lines = path.read_text().splitlines(keepends=True)
out = []
skip_indent = None
for line in lines:
    if skip_indent is not None:
        indent = len(line) - len(line.lstrip())
        if not line.strip() or indent > skip_indent or (indent == skip_indent and line.lstrip().startswith('- ')):
            continue
        skip_indent = None
    match = re.match(r'^(\s*)additionalGids:', line)
    if match:
        skip_indent = len(match.group(1))
        continue
    out.append(re.sub(r'^cdiVersion:.*', 'cdiVersion: 0.6.0', line))
path.write_text(''.join(out))
PYCDI
  grep -qx 'cdiVersion: 0.6.0' "$cdi_tmp" || { echo 'CDI version rewrite failed' >&2; exit 1; }
  install -m 0644 "$cdi_tmp" /etc/cdi/nvidia.yaml
  rm -f "$cdi_tmp"
fi
# Hold installed kernel, initramfs and NVIDIA driver packages to avoid silent measurement drift.
if [[ $DRY == 1 ]]; then
  echo '+ apt-mark hold installed kernel, initramfs and NVIDIA driver packages'
else
  mapfile -t held_packages < <(dpkg-query -W -f='${binary:Package} ${db:Status-Status}\n' | awk '$2 == "installed" && $1 ~ /^(linux-(image|modules|headers|generic)|initramfs-tools|nvidia-(driver|dkms|kernel|compute|utils)|libnvidia-)/ {print $1}')
  ((${#held_packages[@]})) || { echo 'No kernel or driver packages found to hold' >&2; exit 1; }
  apt-mark hold "${held_packages[@]}"
fi
run install -d -m 0700 /etc/sn92 /usr/local/libexec/sn92
# Preserve operator configuration and existing files on reruns; installed templates are reviewed manually.
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
for f in sn92-wg-key sn92-gpu-check sn92-serving; do
  run install -m 0755 "$script_dir/$f" "/usr/local/libexec/sn92/$f"
done
for f in sn92-wg-key.service sn92-gpu-check.service sn92-serving.service sn92-egress.service; do
  run install -m 0644 "$script_dir/$f" "/etc/systemd/system/$f"
done
run install -d -m 0755 /etc/systemd/system/wg-quick@wg0.service.d
run install -m 0644 "$script_dir/wg-order.conf" /etc/systemd/system/wg-quick@wg0.service.d/sn92.conf
run install -m 0755 "$script_dir/sn92-egress-render" /usr/local/libexec/sn92/sn92-egress-render
run install -m 0644 "$script_dir/sn92-egress.nft" /etc/sn92/egress.nft.template
if [[ $DRY == 1 ]]; then
  echo '+ render egress rules when /etc/sn92/egress.conf is configured'
  [[ $SN92_ENABLE_EGRESS_LOCK == 0 ]] || echo '+ enable and start sn92-egress.service'
elif [[ -f /etc/sn92/egress.conf ]]; then
  # Root-controlled configuration: exported to renderer, never printed.
  set -a
  # shellcheck source=/dev/null
  source /etc/sn92/egress.conf
  set +a
  rules_tmp=$(mktemp /etc/sn92/.egress.XXXXXX)
  /usr/local/libexec/sn92/sn92-egress-render /etc/sn92/egress.nft.template > "$rules_tmp"
  nft --check -f "$rules_tmp"
  install -m 0600 "$rules_tmp" /etc/sn92/egress.nft
  rm -f "$rules_tmp"
elif [[ $SN92_ENABLE_EGRESS_LOCK == 1 ]]; then
  echo 'Configure /etc/sn92/egress.conf before enabling lockdown' >&2; exit 1
fi
run systemctl daemon-reload
if [[ $SN92_ENABLE_EGRESS_LOCK == 1 ]]; then
  run systemctl enable --now sn92-egress.service
fi
printf '%s\n' 'Installed templates only; configure and review prerequisites in RUNBOOK.md. Egress service starts only when explicitly enabled.'
