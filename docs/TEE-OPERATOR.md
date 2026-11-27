# TEE nodes for inference mining: operator setup

Rule 10 of [INFERENCE-MINING-V2.md](INFERENCE-MINING-V2.md) has the dates and the bonus. This page explains how to set
up a T1 node our verifier can attest. The T1 setup files are in [`tee/`](../tee) in this repository.

**Current scope:** global TEE nodes (open pots) have no EU location requirement: the verifier for the global pots runs
without the EU location check. The EU layer (rule 8) keeps it; its policy also checks that the node is in the EU
(network round trip and an EU network operator). The policy files in [`tee/policy/`](../tee/policy) are the EU-layer
policies (`require_eu_location: true`). There is no fixed onboarding limit; nodes are registered in the order they arrive.

## What we check

A TEE node is a confidential VM with a confidential-computing GPU. Our verifier asks the VM for fresh evidence and checks:

- the CPU report (AMD SEV-SNP or Intel TDX) against the vendor root certificates and minimum firmware versions;
- the GPU evidence (NVIDIA, confidential computing on) for every GPU in the VM;
- that the report is bound to our nonce, your miner hotkey, the agent's TLS key and the node's WireGuard key, so it
  cannot be replayed or moved to another hotkey;
- the launch measurement against our approved list (T1 = approved stock image, T2 = approved measured image).

A verdict is valid for 6 hours and is refreshed about every hour. If a check fails, the node simply does not count as
TEE until the next good verdict.

## Supported hardware

| CPU TEE | GPU | Status |
| --- | --- | --- |
| AMD SEV-SNP (EPYC Turin) | RTX PRO 6000 Blackwell Server Edition, CC on | T1 setup available on Verda FIN-03. For T2, see below. |
| AMD SEV-SNP (EPYC Genoa / Milan) | as above | on request: not accepted yet (no firmware minimum configured, so the verifier rejects it) |
| AMD SEV-SNP | H100 / H200 / B200, CC on | verifier code ready; policy and image after a first test node |
| Intel TDX (Xeon 5th gen / Xeon 6) | H100 / H200 / B200, CC on | verifier code ready; policy and TDX guest image after a first test node |

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

## T2: confirm the image before renting

T2 uses an approved measured image with a dm-verity root. The verifier checks its exact launch measurement.
For a self-service setup, use the [T1 instructions above](#t1-about-15-minutes-on-a-verda-confidential-vm).
If you need T2, ask in #92 for the current approved image, its artifact hashes and the instructions for
your lane before renting or replacing your boot image.

The 24 November test image and its measurement files are historical. The verifier requires the current
approved measurement for T2. A locally rebuilt image also needs its own approved measurement.

## Privacy

The agent never logs request bodies and never sees validator secrets. Serving images must turn off prompt and
request logging. Never send us private keys, quotes or tokens by chat; the verifier fetches what it needs itself.

## Help

Ask in #92 on the Bittensor Discord.
