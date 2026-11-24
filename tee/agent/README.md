# SN92 in-VM attestation agent

The round-one flow was exercised on a Verda RTX PRO 6000 confidential VM; the changes in this version still need an end-to-end hardware test. The workload is self-reported, not measured. Provision the WireGuard private key inside the VM; the agent binds its public key but does not prove private-key custody. Validators must independently verify the evidence and pin the expected WireGuard key.

On Verda image `24.04.cuda13.4.cc`, whose CUDA apt repository is already configured, install the CLI and Python environment:

```sh
sudo apt-get update
sudo apt-get install -y nvattest python3-venv
sudo mkdir -p /opt/sn92-agent /etc/sn92
sudo python3 -m venv /opt/sn92-agent/venv
sudo /opt/sn92-agent/venv/bin/pip install 'cryptography==50.0.2'
```

Copy this repository to `/opt/sn92-agent/src`. The default executable is `/usr/bin/nvattest`; override it with an absolute `SN92_NVATTEST_PATH` if installed elsewhere. If that executable is absent, the agent falls back to the deprecated NVIDIA Python SDK, which must be installed separately in the same venv (`nv-attestation-sdk==2.7.3`). CLI failures and invalid CLI output do not trigger fallback. No evidence is fabricated.

Create `/etc/sn92/workload.json`, root-owned and not group/world writable, with these fields:

```json
{"serving_image":"registry/name@sha256:<digest>","agent_version":"1.0","runbook_version":"1"}
```

Create root-owned `/etc/sn92/agent.env` with required `SN92_MINER_HOTKEY=<miner SS58 hotkey>` and `SN92_WG_PUBLIC_KEY=<base64 public key>`; the agent refuses to start without a hotkey of SS58 shape and rejects challenges for other hotkeys. Optionally set `SN92_VALIDATOR_ALLOW` to a comma-separated list of allowed source CIDRs (for example `192.0.2.0/24,2001:db8::/32`); an unset variable allows all sources and an empty or invalid list prevents startup. The WireGuard public key an empty value means no WireGuard interface and the node is not routable. Alternatively omit that variable and install `wireguard-tools` to read `wg show wg0 public-key`; set `SN92_WG_INTERFACE` to select another interface. Set `SN92_LISTEN_HOST` and `SN92_LISTEN_PORT` to override the default `0.0.0.0:8443`. `SN92_WG_LISTEN_HOST` optionally adds a second listener on that address and port; the wildcard listener already covers it. Both listeners share a limit of one matching attestation per source IP per 10 seconds and six globally per 10 seconds, as well as a 4 KiB request-body limit. Matching requests count even if evidence collection fails. TLS handshakes have a 10-second timeout, followed by a 30-second total request deadline.

Install `/etc/systemd/system/sn92-attest.service`:

```ini
[Unit]
Description=SN92 in-VM attestation agent
After=network-online.target wg-quick@wg0.service sn92-gpu-check.service
Wants=network-online.target wg-quick@wg0.service sn92-gpu-check.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/sn92-agent/src
RuntimeDirectory=sn92/tls
RuntimeDirectoryMode=0700
Environment=SN92_WORKLOAD_CONFIG=/etc/sn92/workload.json
Environment=SN92_TLS_TMPDIR=/run/sn92/tls
# Set SN92_MINER_HOTKEY and optional SN92_VALIDATOR_ALLOW in agent.env.
EnvironmentFile=/etc/sn92/agent.env
ExecStart=/opt/sn92-agent/venv/bin/python -m agent.server
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/sys/kernel/config/tsm/report /run/sn92/tls
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK

[Install]
WantedBy=multi-user.target
```

Enable and self-test:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now sn92-attest.service
curl -k https://127.0.0.1:8443/healthz
```

The health response is `{"ok":true}`. The key exists in memory and briefly in a private directory on `/run` tmpfs, with files unlinked after loading. The validator must capture the self-signed TLS certificate's SPKI on its own connection and verify the evidence binding. Empty SNP auxiliary data is expected on Verda; the validator must obtain the VCEK independently. This service does not implement the gateway or key release.
