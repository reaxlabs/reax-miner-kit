#!/usr/bin/env bash
# REAX miner kit: one-line installer.
#
#   curl -fsSL https://raw.githubusercontent.com/reaxlabs/reax-miner-kit/v0.2.0/install.sh \
#     | bash -s -- --model <id> [--eu] [--commit <40-hex commit from https://reax.dev/mine/>]
#
# What it does: checks this machine, clones the kit at a pinned tag, checks the commit, validates the release
# descriptor and runs `reaxctl up --model <id>` (weights, runtime image by digest, runtime, self-check, miner, service).
# What it never does: touch a coldkey or recovery phrase, pay anything, or register a hotkey on chain (that is the one
# human step; the output prints it). It runs no sudo unless you pass --install-deps, and then only the apt-get line
# listed in APT_PACKAGES below. Running it again changes nothing and prints the status.
#
# Exit codes follow reaxctl: 0 ok, 2 usage, 10 requirement unmet or needs a person, 13 download failed, 20 verification failed.
#
# The whole script is one function that runs on the last line, so a half-downloaded copy never executes.

set -euo pipefail

# RELEASE: set to the tag this file is published under (the URL above names the same tag).
DEFAULT_TAG="v0.2.0"
KIT_URL="https://github.com/reaxlabs/reax-miner-kit.git"
APT_PACKAGES=(git python3 python3-venv ca-certificates)
DOCKER_DOC="https://docs.docker.com/engine/install/"
NVIDIA_DOC="https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html"

PROBLEMS=()

say() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { local code="$1"; shift; printf 'error: %s\n' "$*" >&2; exit "$code"; }
problem() { PROBLEMS+=("$1"$'\n'"    Fix: $2"); }

usage() {
  cat <<'EOF'
REAX miner kit installer.

Usage: install.sh --model <id> [options]

  --model ID        model to mine (required). List them with: reaxctl models
  --eu              EU contract operator: answer test instead of the bit-identity self-check
  --gpu N           use GPU number N of a multi-GPU machine (numbers start at 0)
  --commit SHA      the commit announced for this tag at https://reax.dev/mine/ (40 hex); the install stops if it differs
  --tag TAG         kit tag to install (default: the tag this script was published under)
  --dir DIR         where the kit is cloned (default: ~/reax-miner-kit)
  --home DIR        reaxctl state directory (default: ~/.reax-miner)
  --mode MODE       mainnet (default) or testnet
  --install-deps    run `sudo apt-get install -y git python3 python3-venv ca-certificates` for missing basics (Debian/Ubuntu only)
  --list            clone the kit and list the models with what each needs and whether this machine fits; install nothing else
  --dry-run         check this machine and print the plan; clone nothing, install nothing
  --no-up           clone and verify the kit only; do not run reaxctl up
  --repo URL        use another kit repository (tests, mirrors); prints a warning
  -h, --help        this text

Giving --model on a mainnet install is your approval to set up a mainnet miner: the installer passes
--confirm-mainnet to reaxctl for you. Nothing is paid and no hotkey is registered by this script.
EOF
}

parse_args() {
  MODEL="" EU=0 GPU="" COMMIT="" TAG="$DEFAULT_TAG" KIT_DIR="${HOME:-}/reax-miner-kit" STATE_HOME="" MODE="mainnet"
  INSTALL_DEPS=0 DRY_RUN=0 NO_UP=0 LIST=0 REPO="$KIT_URL"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --model) [ "$#" -ge 2 ] || die 2 "--model needs a value"; MODEL="$2"; shift 2 ;;
      --eu) EU=1; shift ;;
      --gpu) [ "$#" -ge 2 ] || die 2 "--gpu needs a value"; GPU="$2"; shift 2 ;;
      --commit) [ "$#" -ge 2 ] || die 2 "--commit needs a value"; COMMIT="$2"; shift 2 ;;
      --tag) [ "$#" -ge 2 ] || die 2 "--tag needs a value"; TAG="$2"; shift 2 ;;
      --dir) [ "$#" -ge 2 ] || die 2 "--dir needs a value"; KIT_DIR="$2"; shift 2 ;;
      --home) [ "$#" -ge 2 ] || die 2 "--home needs a value"; STATE_HOME="$2"; shift 2 ;;
      --mode) [ "$#" -ge 2 ] || die 2 "--mode needs a value"; MODE="$2"; shift 2 ;;
      --install-deps) INSTALL_DEPS=1; shift ;;
      --dry-run) DRY_RUN=1; shift ;;
      --no-up) NO_UP=1; shift ;;
      --list) LIST=1; shift ;;
      --repo) [ "$#" -ge 2 ] || die 2 "--repo needs a value"; REPO="$2"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) usage >&2; die 2 "unknown option: $1" ;;
    esac
  done
  [ "$LIST" -eq 1 ] || [ -n "$MODEL" ] || { usage >&2; die 2 "--model is required"; }
  [ -z "$MODEL" ] || [[ "$MODEL" =~ ^[A-Za-z0-9_-]{1,60}$ ]] || die 2 "--model must be a model id such as s1-fast"
  [[ "$TAG" =~ ^v[0-9][A-Za-z0-9._-]*$ ]] || die 2 "--tag must look like v0.2.0"
  [ -z "$COMMIT" ] || [[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || die 2 "--commit must be the full 40-character lower-case commit hash"
  [ -z "$GPU" ] || [[ "$GPU" =~ ^[0-9]{1,2}$ ]] || die 2 "--gpu must be a GPU number such as 0"
  case "$MODE" in mainnet|testnet) ;; *) die 2 "--mode must be mainnet or testnet" ;; esac
  [ -n "$KIT_DIR" ] && [ "$KIT_DIR" != "/reax-miner-kit" ] || die 2 "cannot work out where to put the kit; pass --dir"
  if [ "$REPO" != "$KIT_URL" ]; then warn "using a different kit repository: $REPO (the pinned tag and --commit still apply)"; fi
}

is_debian_like() {
  [ -r /etc/os-release ] || return 1
  # shellcheck disable=SC1091
  ( . /etc/os-release; case " ${ID:-} ${ID_LIKE:-} " in *" debian "*|*" ubuntu "*) exit 0 ;; esac; exit 1 )
}

apt_fix() {
  if is_debian_like; then
    say "sudo apt-get update && sudo apt-get install -y ${APT_PACKAGES[*]}   (or rerun with --install-deps)"
  else
    say "install git and python3 (3.10 or newer, with venv support) with your package manager"
  fi
}

check_base() {
  PROBLEMS=()
  [ "$(uname -s)" = "Linux" ] || die 10 "this installer needs Linux (macOS and native Windows can only act as a controller; use Linux x86_64, or WSL2 with an NVIDIA GPU)."
  case "$(uname -m)" in x86_64|aarch64) ;; *) problem "CPU architecture $(uname -m) is not supported." "use Linux on x86_64 (or aarch64 for CPU and Jetson models)" ;; esac
  command -v git >/dev/null 2>&1 || problem "git is missing." "$(apt_fix)"
  if ! command -v python3 >/dev/null 2>&1; then
    problem "python3 is missing." "$(apt_fix)"
  else
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' || problem "python3 is older than 3.8." "$(apt_fix)"
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || problem "python3 is older than 3.10, which the miner tools need." "install Python 3.10 or newer (Ubuntu 22.04+ and Debian 13 ship one)"
    python3 -c 'import ensurepip' >/dev/null 2>&1 || problem "python3 venv support is missing." "$(apt_fix)"
  fi
}

check_docker() {
  # $1 = gpu | cpu | unknown: whether the chosen model needs an NVIDIA GPU (unknown before the kit is cloned: GPU problems are then not reported)
  local need="${1:-unknown}" out driver
  if ! command -v docker >/dev/null 2>&1; then
    problem "Docker is not installed." "install Docker Engine: $DOCKER_DOC"
    return 0
  fi
  if ! out="$(docker info 2>&1)"; then
    if printf '%s' "$out" | grep -qi 'permission denied'; then
      problem "this user may not use Docker." "sudo usermod -aG docker \"\$USER\"   then log out and back in"
    else
      problem "the Docker daemon is not reachable." "sudo systemctl enable --now docker   (details: $DOCKER_DOC)"
    fi
    return 0
  fi
  if ! docker compose version >/dev/null 2>&1; then
    problem "the Docker Compose plugin is missing." "install it: https://docs.docker.com/compose/install/linux/"
  fi
  if [ "$need" = "gpu" ]; then
    if ! command -v nvidia-smi >/dev/null 2>&1; then
      problem "no NVIDIA GPU or driver found (nvidia-smi is missing)." "install an NVIDIA driver R580 or newer: https://www.nvidia.com/en-us/drivers/"
      return 0
    fi
    driver="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n 1 | cut -d. -f1 || true)"
    if ! [[ "$driver" =~ ^[0-9]+$ ]] || [ "$driver" -lt 580 ]; then
      problem "the NVIDIA driver is too old or not working (found: ${driver:-none}; R580 or newer is needed)." "install a newer driver: https://www.nvidia.com/en-us/drivers/"
      return 0
    fi
    if ! docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -qi nvidia; then
      problem "the NVIDIA container runtime is not configured for Docker." "install the NVIDIA Container Toolkit ($NVIDIA_DOC), then: sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker"
    fi
  fi
  return 0
}

print_problems() {
  [ "${#PROBLEMS[@]}" -gt 0 ] || return 0
  say "This machine is not ready yet:"
  local item i=1
  for item in "${PROBLEMS[@]}"; do
    say "  $i. $item"
    i=$((i + 1))
  done
  say "Fix these, then run the same command again; finished steps are skipped."
  return 1
}

install_deps() {
  is_debian_like || die 10 "--install-deps works on Debian/Ubuntu only. $(apt_fix)"
  if [ "$(id -u)" -eq 0 ]; then
    say "Running: apt-get update && apt-get install -y ${APT_PACKAGES[*]}"
    apt-get update
    apt-get install -y "${APT_PACKAGES[@]}"
  else
    say "Running: sudo apt-get update && sudo apt-get install -y ${APT_PACKAGES[*]}"
    sudo apt-get update
    sudo apt-get install -y "${APT_PACKAGES[@]}"
  fi
}

fetch_kit() {
  if [ -d "$KIT_DIR/.git" ]; then
    local origin
    origin="$(git -C "$KIT_DIR" config --get remote.origin.url || true)"
    [ "$origin" = "$REPO" ] || die 10 "$KIT_DIR is a git checkout of $origin, not $REPO; refusing to touch it. Use --dir for another place."
    [ -z "$(git -C "$KIT_DIR" status --porcelain)" ] || die 10 "$KIT_DIR has local changes; refusing to touch it. Commit or remove them, or use --dir."
    git -C "$KIT_DIR" fetch --quiet --tags origin || die 13 "could not fetch tags from $REPO"
    git -C "$KIT_DIR" rev-parse --verify --quiet "refs/tags/$TAG^{commit}" >/dev/null || die 13 "tag $TAG does not exist in $REPO"
    if [ "$(git -C "$KIT_DIR" rev-parse HEAD)" = "$(git -C "$KIT_DIR" rev-parse "refs/tags/$TAG^{commit}")" ]; then
      say "Kit $TAG is already in $KIT_DIR."
    else
      git -c advice.detachedHead=false -C "$KIT_DIR" checkout --quiet --detach "refs/tags/$TAG" || die 13 "could not check out $TAG"
      say "Kit moved to $TAG in $KIT_DIR."
    fi
  else
    if [ -e "$KIT_DIR" ] && [ -n "$(ls -A "$KIT_DIR" 2>/dev/null || true)" ]; then
      die 10 "$KIT_DIR exists and is not a kit checkout; refusing to overwrite it. Use --dir."
    fi
    git -c advice.detachedHead=false clone --quiet --branch "$TAG" --depth 1 "$REPO" "$KIT_DIR" || die 13 "could not clone $REPO at $TAG (network down, or the tag does not exist)"
    say "Kit $TAG cloned to $KIT_DIR."
  fi
}

verify_kit() {
  local head tagged
  head="$(git -C "$KIT_DIR" rev-parse HEAD)"
  tagged="$(git -C "$KIT_DIR" rev-parse "refs/tags/$TAG^{commit}")"
  [ "$head" = "$tagged" ] || die 20 "the checked-out commit $head is not tag $TAG ($tagged)."
  if [ -n "$COMMIT" ]; then
    [ "$head" = "$COMMIT" ] || die 20 "commit mismatch: tag $TAG is $head but you expected $COMMIT. Do not continue; compare with https://reax.dev/mine/."
    say "Commit verified against the one you gave: $head"
  else
    warn "UNVERIFIED: tag $TAG is commit $head. Compare it with the commit announced at https://reax.dev/mine/ (a second place); or rerun with --commit $head once it matches."
  fi
  # Validate the descriptor bundled in this kit: trusted image owners, sha256 digests, 40-hex weights revisions.
  python3 -I -c '
import json, sys
sys.path.insert(0, sys.argv[1])
from reaxlib import descriptor
descriptor.validate(json.load(open(descriptor.BUNDLED)))
' "$KIT_DIR" || die 20 "the release descriptor bundled in this kit did not validate."
  say "Release descriptor validated."
}

model_need() {
  python3 "$KIT_DIR/reaxctl" models --offline --all --json 2>/dev/null | python3 -c '
import json, sys
rows = json.load(sys.stdin)["data"]["models"]
row = next((r for r in rows if r["id"] == sys.argv[1]), None)
print("unknown" if row is None else ("gpu" if row["accelerator"] == "nvidia" else "cpu"))
' "$MODEL" || echo unknown
}

main() {
  parse_args "$@"
  local extra=""
  [ "$EU" -eq 0 ] || extra=" --eu"
  [ -z "$GPU" ] || extra="$extra --gpu $GPU"
  say "REAX miner kit installer: model $MODEL, kit $TAG, mode $MODE${extra:+, options:$extra}"
  check_base
  if [ "${#PROBLEMS[@]}" -gt 0 ] && [ "$INSTALL_DEPS" -eq 1 ] && [ "$DRY_RUN" -eq 0 ]; then
    install_deps
    check_base
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    check_docker unknown
    say "Dry run. Would do:"
    say "  1. clone $REPO at $TAG into $KIT_DIR and check the commit${COMMIT:+ against $COMMIT}"
    say "  2. validate the release descriptor"
    say "  3. reaxctl up --model $MODEL --mode $MODE$extra (doctor, weights and image, runtime, self-check, miner, service)"
    say "  4. print the registration step for you (no wallet is touched, nothing is paid)"
    print_problems || exit 10
    say "Nothing was changed."
    exit 0
  fi
  print_problems || exit 10
  fetch_kit
  verify_kit
  if [ "$LIST" -eq 1 ]; then
    python3 "$KIT_DIR/reaxctl" models
    say "Next: rerun with --model <id> to set up this machine."
    exit 0
  fi
  if [ "$NO_UP" -eq 1 ]; then
    say "Kit ready in $KIT_DIR. Next: $KIT_DIR/reaxctl up --model $MODEL"
    exit 0
  fi
  local need
  need="$(model_need)"
  [ "$need" != "unknown" ] || { python3 "$KIT_DIR/reaxctl" models --offline; die 2 "unknown model: $MODEL (see the list above)"; }
  PROBLEMS=()
  check_docker "$need"
  print_problems || exit 10
  local args=(up --model "$MODEL" --mode "$MODE")
  [ "$MODE" != "mainnet" ] || args+=(--confirm-mainnet)
  [ "$EU" -eq 0 ] || args+=(--eu)
  [ -z "$GPU" ] || args+=(--gpu "$GPU")
  [ -z "$STATE_HOME" ] || args+=(--home "$STATE_HOME")
  say "Running: reaxctl ${args[*]}"
  exec python3 "$KIT_DIR/reaxctl" "${args[@]}"
}

main "$@"
