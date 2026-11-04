import hashlib
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / 'configs/mammoth-triton-seed/UBI36QQLJDLOXDYUOXUAFH7IJLNLP3IQAUE3ETMXLNEPEZIFLWYQ/chunk_fwd_kernel_o.autotune.json'


class RetiredSeedTest(unittest.TestCase):
    """Setups rendered from main on 4 Nov 2026 mount this file and may run this preflight; a pull must not break them."""

    def test_seed_file_stays_byte_identical(self):
        self.assertEqual(hashlib.sha256(SEED.read_bytes()).hexdigest(),
                         '51b996aa4e577fa923c4d6142b79c613a9aba2e79506dff111277ba8e8b2a326')

    def test_preflight_still_passes(self):
        run = subprocess.run([sys.executable, str(ROOT / 'reaxlib/triton_seed.py')], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_new_setups_no_longer_use_it(self):
        for name in ('service/docker-compose.s1-pro.yml', 'service/reax-s1-pro.service', 'reaxlib/cmd_install.py',
                     'reaxlib/cmd_runtime.py', 'reaxlib/cmd_doctor.py'):
            self.assertNotIn('triton_seed', (ROOT / name).read_text().lower().replace('triton-seed', 'triton_seed'), name)
            self.assertNotIn('REAX_TRITON_SEED', (ROOT / name).read_text(), name)


if __name__ == '__main__':
    unittest.main()
