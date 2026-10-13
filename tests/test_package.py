"""Public distribution invariants, using synthetic sources and tokens only."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('public_package', Path(__file__).resolve().parents[1] / 'scripts/package_skill.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source'
        for relative in ('SKILL.md', 'README.md', 'scripts/workflow.py', 'agents/openai.yaml'):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('synthetic public source', encoding='utf-8')

    def test_runtime_data_is_refused_not_silently_published(self):
        path = self.root / 'audit/result.json'
        path.parent.mkdir()
        path.write_text('{"synthetic":true}')
        report = p.inspect(self.root)
        self.assertEqual(report['status'], 'BLOCKED')
        self.assertIn('not_public_source', [row['reason'] for row in report['findings']])

    def test_secret_detection_never_echoes_the_secret(self):
        token = 'ya29.' + 'SYNTHETIC_NOT_A_REAL_TOKEN' * 2
        (self.root / 'README.md').write_text(token)
        report = p.inspect(self.root)
        self.assertEqual(report['status'], 'BLOCKED')
        self.assertNotIn(token, str(report))

    def test_caches_are_not_packaged_and_archive_is_repeatable(self):
        cache = self.root / 'scripts/__pycache__/fixture.pyc'
        cache.parent.mkdir()
        cache.write_bytes(b'synthetic cache')
        output1 = self.root.parent / 'one.zip'
        output2 = self.root.parent / 'two.zip'
        first = p.package(self.root, output1)
        second = p.package(self.root, output2)
        self.assertEqual(first['status'], 'PASS')
        self.assertEqual(first['archive_sha256'], second['archive_sha256'])
        with zipfile.ZipFile(output1) as archive:
            self.assertFalse(any('__pycache__' in name for name in archive.namelist()))
            self.assertEqual(len(archive.namelist()), 4)

    def test_output_inside_source_or_existing_output_is_refused(self):
        with self.assertRaises(ValueError):
            p.package(self.root, self.root / 'output.zip')
        output = self.root.parent / 'existing.zip'
        output.write_bytes(b'preserved')
        with self.assertRaises(FileExistsError):
            p.package(self.root, output)
        self.assertEqual(output.read_bytes(), b'preserved')


if __name__ == '__main__':
    unittest.main()
