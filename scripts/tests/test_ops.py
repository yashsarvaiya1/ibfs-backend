import importlib.util
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('ops', Path(__file__).parents[1] / 'ops.py')
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


class BackupVerificationTests(unittest.TestCase):
    def make_backup(self, root, member='media/test.txt'):
        (root / 'database.dump').write_bytes(b'database')
        (root / 'accounting.json').write_text('{}')
        with tarfile.open(root / 'media.tgz', 'w:gz') as archive:
            info = tarfile.TarInfo(member)
            archive.addfile(info)
        manifest = {'version': 1, 'files': {name: ops.checksum(root / name) for name in ('database.dump', 'accounting.json', 'media.tgz')}}
        (root / 'manifest.json').write_text(json.dumps(manifest))

    def test_corruption_and_archive_traversal_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_backup(root)
            ops.verify(root)
            (root / 'database.dump').write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError, 'verification failed'):
                ops.verify(root)
            self.make_backup(root, 'media/../../escape')
            with self.assertRaisesRegex(ValueError, 'Unsafe'):
                ops.verify(root)

    def test_manifest_cannot_select_arbitrary_host_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_backup(root)
            (root / 'manifest.json').write_text(json.dumps({'version': 1, 'files': {'../secret': 'hash'}}))
            with self.assertRaisesRegex(ValueError, 'manifest'):
                ops.verify(root)
