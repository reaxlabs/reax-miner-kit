# TEE nodes for inference mining: operator setup

Rule 10 of [INFERENCE-MINING-V2.md](INFERENCE-MINING-V2.md) has the dates and the bonus. This page explains how to set
up a node our validators can attest. Everything you need is in [`tee/`](../tee) in this repository.

## What we check

A TEE node is a confidential VM with a confidential-computing GPU. Our verifier asks the VM for fresh evidence and checks:

- the CPU report (AMD SEV-SNP or Intel TDX) against the vendor root certificates and minimum firmware versions;
- the GPU evidence (NVIDIA, confidential computing on) for every GPU in the VM;
- that the report is bound to our nonce, your miner hotkey, the agent's TLS key and the node's WireGuard key, so it
  cannot be replayed or moved to another hotkey;
- the launch measurement against our approved list (T1 = approved stock image, T2 = our measured image).

A verdict is valid for 6 hours and is refreshed about every hour. If a check fails, the node simply does not count as
TEE until the next good verdict.

## Supported hardware

| CPU TEE | GPU | Status |
| --- | --- | --- |
| AMD SEV-SNP (EPYC Genoa / Turin) | RTX PRO 6000 Blackwell Server Edition, CC on | proven live end to end (T1 and T2) |
| AMD SEV-SNP | H100 / H200 / B200, CC on | verifier supports it; first live node pending |
| Intel TDX (Xeon 5th gen / Xeon 6) | H100 / H200 / B200, CC on | verifier supports it; TDX guest image not built yet |

Whole-machine TEE platforms that attest their own image to their own validators (e.g. TargonOS) cannot be reused: the
report must carry our measurement and our binding. The same hardware works with the VM described here.

## T1: about 15 minutes on a Verda confidential VM

T1 attests a known stock image. It is the fastest way in.

1. Rent a Verda `1RTXPRO6000.30V.CC` (FIN-03) with image `24.04.cuda13.4.cc`. Use at least 100 GB disk if the model
   weights live on the box.
2. Copy this repository to the VM and install the node stack (Podman, NVIDIA CDI spec, `nvattest`); read
   `tee/guest/RUNBOOK.md` first:
   ```sh
   sudo mkdir -p /opt/sn92-agent && sudo cp -r tee /opt/sn92-agent/src
   cd /opt/sn92-agent/src/guest
   export SN92_NVIDIA_KEY_FINGERPRINT='<NVIDIA repo key fingerprint, verified independently>'
   SN92_DRY_RUN=1 ./setup.sh && sudo --preserve-env=SN92_NVIDIA_KEY_FINGERPRINT ./setup.sh
   ```
3. Write `/etc/sn92/config` (serving image digest, CUDA test image, tunnel address) and, if you serve through our
   gateway, `/etc/sn92/wg0.template`. Then run `sn92-gpu-check` (local GPU attestation + CC ready state) and
   `sudo ./sn92-enable` (WireGuard, GPU check, agent and serving start on every boot).
4. Install the attestation agent as in `tee/agent/README.md`, with `SN92_MINER_HOTKEY=<your ss58 hotkey>` in
   `/etc/sn92/agent.env`. It listens on port 8443 (TLS); our verifier must be able to reach it.
5. Register the node: post in #92 that your TEE node is ready, and we tell you how to send your hotkey, the node's
   public IP and the WireGuard public key from `cat /run/sn92/wg-public` (do not post the IP publicly). The key changes
   on every boot, so register it again after a reboot. Counting resumes after the next good verdict.

Podman tips from our live runs: tmpfs mounts need `U` (not `uid=`), and `--read-only` containers break
`nvidia-cdi-hook`.

## T2: our measured image

T2 boots a reproducible image with a dm-verity root (no SSH, no interactive root). Its launch digest is predictable,
so the verifier can check exactly what runs. Build it with `tee/image/build-on-verda.sh` (see `tee/image/DESIGN.md`).
The approved values for the current release are in [`tee/GOLDEN-T2-2026-11-24.json`](../tee/GOLDEN-T2-2026-11-24.json):

| Field | Value |
| --- | --- |
| Platform | Verda `1RTXPRO6000.30V.CC`, image `24.04.cuda13.4.cc`, kernel 6.8.0-149, 30 vCPU EPYC-Turin |
| SNP launch digest | `f7375d2bf39370f66ea5c176b88a918872a91812f25e4905f3767925d56d4f53dca57bd3292604c996755a079fa8e436` |
| dm-verity root hash | `dff1a84385ef7a2221e87970bbbe22b98e4dcaec0408fadb6a7a83ad7031cf4a` |
| Verifier policy | `tee/policy/t2-verda-rtxpro6000-cc-2404-release-f7375d2bf393.json` |

You can recompute the digest yourself with `sev-snp-measure==0.0.13` from the artifact hashes in the golden file. When
T2 becomes the required level we announce it in #92 first; until then T1 and T2 both count as TEE.

## Privacy

The agent never logs request bodies and never sees validator secrets. Serving images must turn off prompt and
request logging. Never send us private keys, quotes or tokens by chat; the verifier fetches what it needs itself.

## Help

Ask in #92 on the Bittensor Discord.
