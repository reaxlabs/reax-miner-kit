import os as _os; _os.environ.setdefault("REAX_OFFLINE", "1")  # tests never touch the network
import pathlib
import sys
import tempfile
import unittest
from reaxlib import cmd_install as io, localnet_ops


class ExtractionTest(unittest.TestCase):
    def fetch_adapter(self, script, root):
        return io.runner([sys.executable, '-c', localnet_ops.FETCH, str(script), str(root)])
    def test_absolute_image_link_is_contained(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / 'rootfs'
            script = pathlib.Path(tmp) / 'source-fetch.py'
            script.write_text('''import io,sys,tarfile
buf=io.BytesIO()
with tarfile.open(fileobj=buf,mode='w') as t:
    f=tarfile.TarInfo('usr/lib/library'); f.size=1; t.addfile(f,io.BytesIO(b'x'))
    link=tarfile.TarInfo('lib64/library'); link.type=tarfile.SYMTYPE; link.linkname='/usr/lib/library'; t.addfile(link)
buf.seek(0)
with tarfile.open(fileobj=buf) as t: t.extractall(sys.argv[1],filter='data')
''')
            rc, _, err = self.fetch_adapter(script, root)
            self.assertEqual(rc, 0, err)
            self.assertEqual((root/'lib64/library').read_text(), 'x')
            self.assertTrue(str((root/'lib64/library').resolve()).startswith(str(root)))
    def test_archive_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / 'rootfs'
            script = pathlib.Path(tmp) / 'source-fetch.py'
            script.write_text('''import io,sys,tarfile
buf=io.BytesIO()
with tarfile.open(fileobj=buf,mode='w') as t:
    f=tarfile.TarInfo('../escape'); f.size=1; t.addfile(f,io.BytesIO(b'x'))
buf.seek(0)
with tarfile.open(fileobj=buf) as t: t.extractall(sys.argv[1],filter='data')
''')
            self.assertNotEqual(self.fetch_adapter(script, root)[0], 0)
            self.assertFalse((pathlib.Path(tmp)/'escape').exists())
