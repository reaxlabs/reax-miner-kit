# Confidential GPU guest: operator runbook (T1 prototype)

**Not production-ready.** T1 is operator-controlled and does not prove the software or protect customer data from operator root. A verified remote verdict, an approved serving image and a working v2 agent are required before customer traffic. `PROBE-1.md` records the live observations. The installer and boot chain passed live on four Verda CC VMs on 24 Nov (incl. the isolated staging E2E); the egress firewall has not been tested live yet. The binding contract is implemented in `agent/` and `teeverify/binding.py`.

## Platform and installation

The live probe used Verda `1RTXPRO6000.30V.CC`, FIN-03, image `24.04.cuda13.4.cc` (Ubuntu 24.04.5, kernel 6.8.0-149, SNP Turin, RTX PRO 6000, driver 595.91.07). The CUDA apt repository was preconfigured. Confirm the EU site, GPU and CPU evidence independently; `nvidia-smi conf-compute -f` reporting CC ON and `/sys/kernel/config/tsm/report` existing do not themselves verify a quote.

Review `setup.sh` and all templates. It defaults to Ubuntu archive Podman 4.9.3, NVIDIA toolkit 1.20.2-1 from NVIDIA's apt repository, nvattest 2.0.0 from the preconfigured CUDA repo, and driver 595.91.07. Override `SN92_PODMAN_VERSION`, `SN92_TOOLKIT_VERSION`, `SN92_NVATTEST_VERSION`, and `SN92_DRIVER_VERSION` as needed; package versions must be available. The Podman and nvattest pins select an apt revision matching the stated upstream version. Verify NVIDIA's full repository signing-key fingerprint through an independent trusted source.

```sh
export SN92_NVIDIA_KEY_FINGERPRINT='<40 independently verified hex digits>'
SN92_DRY_RUN=1 ./setup.sh
sudo --preserve-env=SN92_NVIDIA_KEY_FINGERPRINT ./setup.sh
```

Preserve any overrides with `sudo --preserve-env` too. The installer checks the driver and CC state, installs pinned packages, masks and stops `nvidia-cdi-refresh.service` and `.path`, removes `/var/run/cdi/nvidia.yaml`, and generates `/etc/cdi/nvidia.yaml` with CDI version 0.6.0 and no `additionalGids`. This is needed because Podman 4.9.3 rejects the newer generated spec. Check `nvidia-ctk cdi list` and test the device in a reviewed container after local attestation and readiness.

The installer holds installed kernel, initramfs and NVIDIA driver packages with `apt-mark hold`. In the probe, Verda direct-booted the guest's kernel and initrd; changing the initrd changed the SNP launch digest. A hold reduces accidental drift but is not an integrity guarantee: firmware, boot arguments and manual initrd changes can also change measurements. Plan security updates deliberately, compute and approve a new golden measurement, then unhold, update, remeasure and re-attest before routing. Holds do not protect `/boot` from root.

## Configuration and readiness

Pull approved, digest-pinned serving and CUDA test images during provisioning. Set `/etc/sn92/config` root-owned mode 600 with shell-safe values; the helpers source it as root:

```sh
SN92_SERVING_IMAGE='registry.example/serving@sha256:<64 lowercase hex>'
SN92_CUDA_TEST_IMAGE='registry.example/cuda@sha256:<64 lowercase hex>'
SN92_WG_IPV4='<assigned tunnel IPv4>'
```

The approved image must disable request/prompt logging and listen on port 8080. Verify effective logging and forwarding with synthetic requests. Review the local `nvattest` policy and trust material. `sn92-gpu-check` requires CC ON and a successful local `nvattest` before `nvidia-smi conf-compute -srs 1`; serving requires that service. The probe verified `nvattest --format json collect-evidence`, not the existing local verifier invocation or the pre-ready container check. Validate both on hardware and stop if either fails; do not bypass them. Readiness is not a remote verdict.

Create `/etc/sn92/wg0.template` root-owned mode 600, without `[Interface]` or a private key, using the gateway's assigned `Address`, peer `PublicKey`, `AllowedIPs`, `Endpoint` and `PersistentKeepalive`. `sn92-wg-key` creates the private key in `/run/sn92` on boot and writes its public key to `/run/sn92/wg-public`. Register the new public key after each boot and wait for a fresh bound verdict. Never send the private key.

Install and configure the lead-reviewed v2 agent and its unit using `agent/README.md`; the old guest agent is v1 and does not implement the binding contract. Start WireGuard, the agent, and serving only after reviewing their actual configuration and local checks. The gateway must enforce the verdict, current policy, expiry and registered WireGuard key; service state alone does not gate traffic. Do not share quotes, keys, tokens or request bodies in logs.

## Egress lockdown

The installed nftables template has named IPv4/IPv6 gateway and service sets, default-drop input, output and forwarding, and accepts gateway traffic on wg0. It permits only gateway UDP and HTTP(S) to the configured attestation hosts, plus established traffic and loopback. Forwarding permits gateway ingress to serving and its established replies; containers have no general outbound access. Other firewall tables may further restrict these flows. DNS, NTP, SSH, package registries and metadata are deliberately not permitted after activation. Ensure out-of-band console recovery and validate all actual flows before use.

Before setup, create `/etc/sn92/egress.conf` mode 600, owned by root, with shell-safe values and the exact reviewed DNS names (space-separated) and WireGuard endpoint port:

```sh
SN92_GATEWAY_HOST='gateway.example'
SN92_GATEWAY_PORT='51820'
SN92_NVIDIA_NRAS_HOSTS='nras.example'
SN92_NVIDIA_RIM_HOSTS='rim.example'
SN92_NVIDIA_OCSP_HOSTS='ocsp.example'
SN92_AMD_KDS_HOSTS='kdsintf.amd.com'
SN92_INTEL_PCS_HOSTS='api.trustedservices.intel.com'
```

Example NVIDIA names are placeholders, not verified endpoints. Setup resolves every configured name through the system resolver; a missing or unresolvable category aborts rendering. It writes `/etc/sn92/egress.nft` and checks syntax; it only enables and starts the firewall when `SN92_ENABLE_EGRESS_LOCK=1` is explicitly supplied. For example, after inspecting the rendered file and establishing console access, rerun with `sudo --preserve-env=SN92_NVIDIA_KEY_FINGERPRINT,SN92_ENABLE_EGRESS_LOCK ./setup.sh` after exporting the switch. This immediately applies the rules and can disconnect management. Existing connections allowed by other firewall chains remain subject to this chain's rules. Verify IPv4, IPv6, WireGuard, GPU checks and forwarding from a container on the target; Podman's networking and NAT require live testing.

Resolved addresses are static, not DNS-based nft rules. Refresh them before DNS changes or expiry: from the recovery console, temporarily remove the SN92 table to restore DNS, rerun setup without the enable switch, review the new file, then restart `sn92-egress.service`; otherwise service can fail closed. Removing the table temporarily removes this lockdown, so do it only during a controlled maintenance period. No automatic refresh or DNS exception is installed. The template itself is not a measured T2 image property.

Disable swap and core dumps persistently, use tmpfs for transient data, and verify logs and scratch storage separately. These are operator controls, not proof against operator root.

## Boot chain (live finding 24 Nov)

After the configuration files exist, run `sudo ./sn92-enable` once. It enables and starts `wg-quick@wg0` (which first runs
`sn92-wg-key`), `sn92-gpu-check`, `sn92-attest` and `sn92-serving`, so they come back after every reboot. The WireGuard key
is new on every boot: send the printed public key to SN92 after each reboot; routing resumes after the next verdict.
