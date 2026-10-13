"""DACL copying tests use API mocks and empty isolated workspace files only."""
import ctypes
from ctypes import wintypes
import importlib.util
import os
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location(
    'file_security_subject', Path(__file__).resolve().parents[1] / 'scripts/file_security.py')
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)


class FileSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.txt'
        self.source.write_bytes(b'synthetic fixture; source ACL never edited')
        self.replacement = self.root / 'replacement.tmp'
        self.replacement.touch()

    def copy_mock(self, protected=False, snapshots=None, success=True):
        original = s._Security(ctypes.create_string_buffer(b'synthetic SD'), protected, (2, (b'ace',)))
        api = SimpleNamespace(SetFileSecurityW=mock.Mock(return_value=success))
        with mock.patch.object(s, 'WINDOWS', True), mock.patch.object(s, '_load_api', return_value=api), \
             mock.patch.object(s, '_read_security', side_effect=snapshots or [original, original, original]), \
             mock.patch.object(s.ctypes, 'get_last_error', return_value=5, create=True):
            s.preserve_replacement_security(self.source, self.replacement)
        return api

    def test_protected_dacl_and_only_dacl_flags_are_copied(self):
        api = self.copy_mock(protected=True)
        call = api.SetFileSecurityW.call_args.args
        self.assertEqual(call[0], str(self.replacement))
        self.assertEqual(call[1], s.DACL_SECURITY_INFORMATION | s.PROTECTED_DACL_SECURITY_INFORMATION)
        self.assertEqual(self.replacement.read_bytes(), b'')
        self.assertEqual(self.source.read_bytes(), b'synthetic fixture; source ACL never edited')

    def test_unprotected_dacl_state_is_explicitly_copied(self):
        api = self.copy_mock()
        self.assertEqual(api.SetFileSecurityW.call_args.args[1],
                         s.DACL_SECURITY_INFORMATION | s.UNPROTECTED_DACL_SECURITY_INFORMATION)

    def test_set_acl_failure_has_no_permissive_fallback(self):
        with self.assertRaises(OSError):
            self.copy_mock(success=False)
        self.assertEqual(self.replacement.read_bytes(), b'')

    def test_protection_or_dacl_readback_mismatch_stops(self):
        original = s._Security(None, True, (2, (b'ace',)))
        for observed in [s._Security(None, False, original.dacl), s._Security(None, True, (2, (b'other',)))]:
            with self.subTest(observed=observed), self.assertRaises(OSError):
                self.copy_mock(snapshots=[original, observed, original])

    def test_concurrent_source_acl_change_is_rejected(self):
        original = s._Security(None, False, (2, (b'ace',)))
        changed = s._Security(None, False, (2, (b'concurrent',)))
        with self.assertRaises(OSError):
            self.copy_mock(snapshots=[original, original, changed])

    def test_absent_source_does_not_change_replacement(self):
        with mock.patch.object(s, '_load_api') as load:
            s.preserve_replacement_security(self.root / 'absent', self.replacement)
        load.assert_not_called()
        self.assertEqual(self.replacement.read_bytes(), b'')

    def test_identical_or_nonempty_replacement_is_rejected_before_api_calls(self):
        with mock.patch.object(s, '_load_api') as load:
            with self.assertRaises(OSError):
                s.preserve_replacement_security(self.source, self.source)
            self.replacement.write_bytes(b'already contains content')
            with self.assertRaises(OSError):
                s.preserve_replacement_security(self.source, self.replacement)
        load.assert_not_called()

    def test_hardlink_to_source_is_rejected_without_editing_source(self):
        linked = self.root / 'hardlink.tmp'
        try:
            os.link(self.source, linked)
        except OSError:
            self.skipTest('Filesystem does not permit fixture hardlinks')
        with self.assertRaises(OSError):
            s.preserve_replacement_security(self.source, linked)

    def test_descriptor_api_failure_is_not_ignored(self):
        api = SimpleNamespace(GetFileSecurityW=mock.Mock(return_value=False))
        with mock.patch.object(s.ctypes, 'get_last_error', return_value=5, create=True), self.assertRaises(OSError):
            s._read_security(api, self.source)

    def test_descriptor_ace_comparison_ignores_unused_padding(self):
        # Synthetic self-relative storage: header area plus an ACL and one ACE.
        raw = bytes(20) + struct.pack('<BBHHH', 2, 0, 16, 1, 0) + struct.pack('<BBH', 0, 0, 4) + b'pad!'
        def get_file(path, info, buffer, length, needed):
            ctypes.cast(needed, ctypes.POINTER(wintypes.DWORD)).contents.value = len(raw)
            if buffer is None:
                return False
            ctypes.memmove(buffer, raw, len(raw))
            return True
        def get_control(buffer, control, revision):
            ctypes.cast(control, ctypes.POINTER(wintypes.WORD)).contents.value = s.SE_DACL_PROTECTED
            ctypes.cast(revision, ctypes.POINTER(wintypes.DWORD)).contents.value = 1
            return True
        def get_dacl(buffer, present, dacl, defaulted):
            ctypes.cast(present, ctypes.POINTER(wintypes.BOOL)).contents.value = 1
            ctypes.cast(defaulted, ctypes.POINTER(wintypes.BOOL)).contents.value = 0
            ctypes.cast(dacl, ctypes.POINTER(ctypes.c_void_p)).contents.value = ctypes.addressof(buffer) + 20
            return True
        api = SimpleNamespace(GetFileSecurityW=get_file, IsValidSecurityDescriptor=lambda value: True,
                              GetSecurityDescriptorControl=get_control, GetSecurityDescriptorDacl=get_dacl)
        with mock.patch.object(s.ctypes, 'get_last_error', return_value=122, create=True):
            observed = s._read_security(api, self.source)
        self.assertTrue(observed.protected)
        self.assertEqual(observed.dacl, (2, (struct.pack('<BBH', 0, 0, 4),)))

    @unittest.skipUnless(os.name == 'nt', 'Real DACL readback is Windows-only')
    def test_real_windows_copy_changes_only_empty_workspace_replacement(self):
        api = s._load_api()
        before = s._read_security(api, self.source)
        s.preserve_replacement_security(self.source, self.replacement)
        after = s._read_security(api, self.source)
        replacement = s._read_security(api, self.replacement)
        self.assertEqual((after.protected, after.dacl), (before.protected, before.dacl))
        self.assertEqual((replacement.protected, replacement.dacl), (before.protected, before.dacl))
        self.assertEqual(self.replacement.read_bytes(), b'')

    @unittest.skipIf(os.name == 'nt', 'POSIX utility mode copy is not Windows ACL enforcement')
    def test_posix_mode_copy_does_not_claim_windows_acl_support(self):
        mode = self.source.stat().st_mode & 0o7777
        s.preserve_replacement_security(self.source, self.replacement)
        self.assertEqual(self.replacement.stat().st_mode & 0o7777, mode)


if __name__ == '__main__':
    unittest.main()
