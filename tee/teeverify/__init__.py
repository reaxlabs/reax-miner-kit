"""teeverify: SN92 EU-tier TEE evidence verifier (prototype, Nov 2026).

Modules:
  tdx      Intel TDX quote v4/v5 parsing, DCAP verification (dcap-qvl), policy
  snp      AMD SEV-SNP attestation report parsing, VCEK/ASK/ARK chain, policy
  nvidia   NVIDIA H100/H200/B200 CC evidence: local SPDM report check and NRAS EAT token check
  binding  report_data / GPU nonce composition that binds a session to one enclave
  location EU location evidence (chip registry + RTT physics bound)
  keyrelease  wraps a customer data key to an attested enclave key (X25519 + HKDF + AES-GCM)
  bundle   one-call verification of a miner evidence bundle against a policy
"""
__version__ = "0.1.0"
