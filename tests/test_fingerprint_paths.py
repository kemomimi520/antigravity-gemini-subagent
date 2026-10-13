"""Match the native agy/classic storage discovery; fixtures stay in the workspace."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('w_paths', Path(__file__).resolve().parents[1] / 'scripts/workflow.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


class FingerprintPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = SimpleNamespace(directory=self.root / 'manager')
        self.manager.directory.mkdir()
        self.config = self.manager.directory / 'gui_config.json'
        self.config.write_text('{}', encoding='utf-8')
        self.appdata = self.root / 'appdata'
        self.env = mock.patch.dict(w.os.environ, {'APPDATA': str(self.appdata), 'LOCALAPPDATA': str(self.root / 'local'),
                       'ProgramFiles': str(self.root / 'programs'), 'ProgramFiles(x86)': str(self.root / 'programs-x86')})
        self.env.start()
        self.addCleanup(self.env.stop)

    def storage(self, directory):
        path = directory / 'User/globalStorage/storage.json'
        path.parent.mkdir(parents=True)
        path.write_text('{}', encoding='utf-8')
        return path.resolve()

    def test_ide_only_storage_is_not_used_for_agy(self):
        self.storage(self.appdata / 'Antigravity IDE')
        ide_exe = self.root / 'local/Programs/Antigravity IDE/Antigravity IDE.exe'
        ide_exe.parent.mkdir(parents=True)
        ide_exe.write_bytes(b'synthetic')
        self.storage(ide_exe.parent / 'data/user-data')
        self.assertEqual(w.fingerprint_storage_paths(self.manager, process_records=[]), [])

    def test_classic_storage_wins_without_an_ide_fallback(self):
        self.storage(self.appdata / 'Antigravity IDE')
        classic = self.storage(self.appdata / 'Antigravity')
        self.assertEqual(w.fingerprint_storage_paths(self.manager, process_records=[]), [classic])

    def test_configured_data_takes_priority_over_active_and_standard(self):
        configured = self.root / 'configured'
        target = self.storage(configured)
        self.config.write_text(json.dumps({'antigravity_args': ['--user-data-dir', str(configured)]}))
        self.storage(self.appdata / 'Antigravity')
        active = self.root / 'active'
        self.storage(active)
        self.assertEqual(w.fingerprint_storage_paths(self.manager, process_records=[{'executable': None, 'user_data_dir': str(active)}]), [target])

    def test_active_data_takes_priority_over_portable_and_standard(self):
        active = self.root / 'active'
        target = self.storage(active)
        exe = self.root / 'installation/Antigravity.exe'
        exe.parent.mkdir()
        exe.write_bytes(b'synthetic')
        self.storage(exe.parent / 'data/user-data')
        self.storage(self.appdata / 'Antigravity')
        self.assertEqual(w.fingerprint_storage_paths(self.manager, process_records=[{'executable': str(exe), 'user_data_dir': str(active)}]), [target])

    def test_portable_takes_priority_over_standard(self):
        exe = self.root / 'installation/Antigravity.exe'
        exe.parent.mkdir()
        exe.write_bytes(b'synthetic')
        self.config.write_text(json.dumps({'antigravity_executable': str(exe)}))
        target = self.storage(exe.parent / 'data/user-data')
        self.storage(self.appdata / 'Antigravity')
        self.assertEqual(w.fingerprint_storage_paths(self.manager, process_records=[]), [target])

    def test_distinct_active_classic_instances_remain_ambiguous(self):
        with self.assertRaisesRegex(w.WorkflowError, 'FINGERPRINT_LOCATION_AMBIGUOUS'):
            w.fingerprint_storage_paths(self.manager, process_records=[
                {'executable': str(self.root / 'one.exe'), 'user_data_dir': None},
                {'executable': str(self.root / 'two.exe'), 'user_data_dir': None}])
