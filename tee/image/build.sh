#!/usr/bin/env bash
set -euo pipefail
# An incomplete image must never be mistaken for a releasable measured image.
if [[ ${1:-} == --dry-run && $# == 1 ]]; then
  printf '%s\n' 'DRY RUN: release build disabled; see image/DESIGN.md for missing inputs and implementation.'
  exit 0
fi
printf '%s\n' 'ERROR: no complete locked rootfs, initramfs, NVIDIA stack or measured workload is available; refusing to build.' >&2
exit 1
