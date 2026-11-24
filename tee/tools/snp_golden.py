#!/usr/bin/env python3
"""Compute a launch digest with the separately installed sev-snp-measure package."""
import argparse
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ('ovmf', 'kernel', 'initrd', 'cmdline', 'vcpus', 'vcpu-type'):
        parser.add_argument('--' + arg, required=True)
    a = parser.parse_args()
    subprocess.run(['sev-snp-measure', '--mode', 'snp', '--vcpus', a.vcpus, '--vcpu-type', a.vcpu_type,
                    '--ovmf', a.ovmf, '--kernel', a.kernel, '--initrd', a.initrd, '--append', a.cmdline], check=True)


if __name__ == '__main__':
    main()
