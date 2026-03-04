import os
import io
import zipfile
import unittest
import tempfile
from pathlib import Path

# Обеспечиваем импорт пакета проекта из корня
import sys
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import mongomock  # type: ignore
from lib.cache import ArtifactCache  # noqa: E402


def make_zip_bytes(files):
    """Утилита для создания zip-байтов из словаря name->bytes|str."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            if isinstance(data, str):
                data = data.encode('utf-8')
            z.writestr(name, data)
    return buf.getvalue()


class TestArtifactCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Mongo mock client
        self.mock_client = mongomock.MongoClient()
        self.cache = ArtifactCache(client=self.mock_client)
        self.owner = 'owner'
        self.repo = 'repo'
        self.run_id = 12345

    def test_sidecar_save_and_load(self):
        # До сохранения — None
        self.assertIsNone(self.cache.load_parsed_sidecar(self.owner, self.repo, self.run_id))

        details = {
            'testA | desc': [
                {'file': 'log.txt', 'line_num': 0, 'context': 'ctx', 'project': 'proj'}
            ],
            'a.b | tricky': [  # ключ с точкой должен корректно сохраниться через список
                {'file': 'log2.txt', 'line_num': 1, 'context': 'ctx2', 'project': 'proj2'}
            ]
        }
        ok = self.cache.save_parsed_sidecar(self.owner, self.repo, self.run_id, details, has_no_tests=True)
        self.assertTrue(ok)

        loaded = self.cache.load_parsed_sidecar(self.owner, self.repo, self.run_id)
        self.assertIsNotNone(loaded)
        det_loaded, no_tests = loaded
        self.assertTrue(no_tests)
        self.assertEqual(det_loaded, details)

    def test_sidecar_upsert(self):
        details1 = {
            't1 | d1': [{'file': '1', 'line_num': 0, 'context': 'a', 'project': 'p'}]
        }
        ok1 = self.cache.save_parsed_sidecar(self.owner, self.repo, self.run_id, details1, has_no_tests=False)
        self.assertTrue(ok1)

        details2 = {
            't2 | d2': [{'file': '2', 'line_num': 1, 'context': 'b', 'project': 'p2'}]
        }
        ok2 = self.cache.save_parsed_sidecar(self.owner, self.repo, self.run_id, details2, has_no_tests=False)
        self.assertTrue(ok2)

        det_loaded, no_tests = self.cache.load_parsed_sidecar(self.owner, self.repo, self.run_id)
        self.assertFalse(no_tests)
        self.assertEqual(det_loaded, details2)

    def test_save_txt_from_zip(self):
        z = make_zip_bytes({
            'a.txt': 'A',
            'nested/b.txt': 'BB',
            'c.bin': b'\x00\x01',
        })
        save_dir = Path(self.tmp.name) / 'logs'
        count = self.cache.save_txt_from_zip(z, save_dir, run_prefix='run42')
        self.assertEqual(count, 2)

        f1 = save_dir / 'run42_a.txt'
        f2 = save_dir / 'run42_nested_b.txt'
        self.assertTrue(f1.exists())
        self.assertTrue(f2.exists())
        self.assertEqual(f1.read_bytes(), b'A')
        self.assertEqual(f2.read_bytes(), b'BB')
        self.assertFalse((save_dir / 'c.bin').exists())


if __name__ == '__main__':
    unittest.main()
