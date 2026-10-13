"""Bundled account-core behavior with synthetic credentials and workspace files."""
import contextlib
import copy
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

w = load('native_test_workflow', ROOT / 'scripts/workflow.py')
n = load('native_test_accounts', ROOT / 'scripts/native_accounts.py')


class FakeGoogle:
    def __init__(self):
        self.calls = []
        self.failure = None

    def call(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.failure:
            raise self.failure
        if url == n.USERINFO_URL:
            return {'email': 'a@example.test', 'verified_email': True, 'name': 'Synthetic'}
        return {'access_token': 'new-synthetic-access', 'expires_in': 3600, 'id_token': 'new-synthetic-id'}

    def cloud(self, operation, token, payload):
        self.calls.append((operation, token, payload))
        if self.failure:
            raise self.failure
        if operation == 'loadCodeAssist':
            return {'cloudaicompanionProject': 'synthetic-project', 'currentTier': {'id': 'free-tier'}}
        if operation == 'fetchAvailableModels':
            return {'models': {'gemini-synthetic': {'quotaInfo': {'remainingFraction': 0.8, 'resetTime': 'later'}}}}
        return {'groups': [{'displayName': 'Gemini', 'buckets': [
            {'bucketId': 'gemini-weekly', 'window': 'weekly', 'remainingFraction': 0.3, 'resetTime': 'later'}]}]}


class NativeAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.google = FakeGoogle()
        self.store = n.NativeAccounts(self.root / 'accounts', w, transport=self.google,
                                     credential_directory=self.root / 'credentials', storage_paths=[])
        self.a = self.account('a')
        self.b = self.account('b')
        self.index = {'version': '2.0', 'accounts': [{'id': key, 'email': key + '@example.test', 'custom': {'preserve': True}} for key in ('a', 'b')],
                      'current_account_id': 'a', 'current_target_ide': 'agy', 'unknown_future_metadata': [1, 2]}
        for account in (self.a, self.b):
            self.write(self.store.account_path(account['id']), account)
        self.write(self.store.directory / 'accounts.json', self.index)
        self.current = self.payload(self.a)
        self.seed_files(self.a)

    def account(self, identifier):
        return {'id': identifier, 'email': identifier + '@example.test', 'token': {
            'access_token': 'synthetic-access-' + identifier, 'refresh_token': 'synthetic-refresh-' + identifier,
            'expiry_timestamp': int(time.time()) + 7200, 'id_token': 'synthetic-id-' + identifier},
            'device_profile': n.new_profile(), 'custom': {'keep': True}, 'disabled': False,
            'validation_blocked': False, 'quota': {'last_updated': 1, 'models': [], 'is_forbidden': False}}

    def write(self, path, value):
        w.atomic_write(path, w.json_bytes(value), private=True)

    def payload(self, account):
        result = w.build_cli_payload(account)
        result['id_token'] = account['token']['id_token']
        return result

    def seed_files(self, account):
        token = account['token']
        self.write(self.store.credential_directory / 'antigravity-cli/antigravity-oauth-token', self.payload(account))
        self.write(self.store.credential_directory / 'oauth_creds.json', {
            'access_token': token['access_token'], 'refresh_token': token['refresh_token'], 'token_type': 'Bearer',
            'expiry_date': token['expiry_timestamp'] * 1000, 'id_token': token['id_token']})
        self.write(self.store.credential_directory / 'google_accounts.json', {'active': account['email'], 'old': []})

    @contextlib.contextmanager
    def credentials(self):
        def read():
            return copy.deepcopy(self.current)
        def write(payload):
            self.current = copy.deepcopy(payload)
        with mock.patch.object(w, 'read_cli_credential_payload', side_effect=read), \
             mock.patch.object(w, 'write_cli_credential_payload', side_effect=write):
            yield read, write

    def test_inventory_works_without_gui_config_or_service(self):
        self.assertFalse((self.store.directory / 'gui_config.json').exists())
        self.assertEqual(len(self.store.accounts()), 2)
        self.assertEqual(self.google.calls, [])

    def test_empty_store_has_actionable_enrollment_diagnostic(self):
        empty = n.NativeAccounts(self.root / 'empty', w)
        with self.assertRaisesRegex(w.WorkflowError, 'register-current'):
            empty.accounts()

    def test_invalid_and_duplicate_ids_are_rejected(self):
        for identifiers in (['a', 'a'], ['../escape'], [{}]):
            with self.subTest(identifiers=identifiers):
                index = {**self.index, 'accounts': [{'id': value} for value in identifiers]}
                self.write(self.store.directory / 'accounts.json', index)
                with self.assertRaises(w.WorkflowError):
                    self.store.index()

    def test_fresh_seconds_and_milliseconds_tokens_need_no_network(self):
        for multiplier in (1, 1000):
            token = {**self.a['token'], 'expiry_timestamp': (int(time.time()) + 3600) * multiplier}
            self.assertEqual(self.store.fresh_token(token), token)
        self.assertEqual(self.google.calls, [])

    def test_expired_token_keeps_refresh_and_optional_identity_metadata(self):
        token = {**self.a['token'], 'expiry_timestamp': 1, 'project_id': 'preserved-project', 'custom': True}
        refreshed = self.store.fresh_token(token)
        self.assertEqual(refreshed['refresh_token'], token['refresh_token'])
        self.assertEqual(refreshed['project_id'], token['project_id'])
        self.assertTrue(refreshed['custom'])
        self.assertEqual(refreshed['id_token'], 'new-synthetic-id')

    def test_legacy_account_without_project_is_not_enterprise_locked(self):
        token = {**self.a['token'], 'expiry_timestamp': 1}
        refreshed = self.store.fresh_token(token)
        self.assertNotIn('oauth_client_key', refreshed)

    def test_invalid_expiry_and_access_do_not_make_requests(self):
        for value in (float('nan'), True, None):
            with self.assertRaises(w.WorkflowError):
                self.store.fresh_token({**self.a['token'], 'expiry_timestamp': value})
        with self.assertRaises(w.WorkflowError):
            self.store.fresh_token({**self.a['token'], 'access_token': ''})
        for field in ('refresh_token', 'id_token', 'oauth_client_key'):
            with self.assertRaises(w.WorkflowError):
                self.store.fresh_token({**self.a['token'], field: True})
        self.assertEqual(self.google.calls, [])

    def test_quota_windows_and_account_metadata_are_preserved(self):
        self.store.refresh_account('a')
        account = self.store.account('a')
        self.assertEqual(account['custom'], self.a['custom'])
        self.assertEqual(account['device_profile'], self.a['device_profile'])
        ranked = w.rank_accounts(self.store.accounts(), 'gemini-synthetic')
        row = next(item for item in ranked['accounts'] if item['account_ref'] == w.account_ref('a'))
        self.assertEqual(row['score_percent'], 30)
        self.assertTrue(row['eligible'])

    def test_failed_refresh_does_not_turn_stale_quota_into_fresh_quota(self):
        self.google.failure = w.WorkflowError('ACCOUNT_NETWORK_UNAVAILABLE')
        before = self.store.account_path('a').read_bytes()
        with self.assertRaises(w.WorkflowError):
            self.store.refresh_account('a')
        self.assertEqual(self.store.account_path('a').read_bytes(), before)

    def test_tiered_quota_maps_effort_variants_without_changing_requested_model(self):
        self.store.refresh_account('a')
        account = self.store.account('a')
        account['quota']['models'][0]['name'] = 'gemini-future-flash-tiered'
        self.write(self.store.account_path('a'), account)
        for effort in ('low', 'medium', 'high'):
            requested = 'gemini-future-flash-' + effort
            result = w.rank_accounts(self.store.accounts(), requested)
            row = next(item for item in result['accounts'] if item['account_ref'] == w.account_ref('a'))
            self.assertEqual(result['model'], requested)
            self.assertEqual(row['quota_model'], 'gemini-future-flash-tiered')
            self.assertTrue(row['eligible'])
            self.assertEqual(row['score_percent'], 30)

    def test_tiered_mapping_does_not_cross_families_or_bypass_model_protection(self):
        self.store.refresh_account('a')
        account = self.store.account('a')
        account['quota']['models'][0]['name'] = 'gemini-future-flash-tiered'
        self.write(self.store.account_path('a'), account)
        result = w.rank_accounts(self.store.accounts(), 'gemini-other-flash-high')
        self.assertEqual(result['status'], 'NO_ELIGIBLE_ACCOUNT')
        for field, value in [('protected_models', ['gemini-future-flash-tiered']),
                             ('live_limited_models', {'gemini-future-flash-tiered': {'until': 0}})]:
            changed = copy.deepcopy(account)
            changed[field] = value
            self.write(self.store.account_path('a'), changed)
            self.assertEqual(w.rank_accounts(self.store.accounts(), 'gemini-future-flash-high')['status'], 'NO_ELIGIBLE_ACCOUNT')

    def test_verified_reauthorization_keeps_account_id_and_device_binding(self):
        self.current['token']['refresh_token'] = 'new-authorized-synthetic-refresh'
        with self.credentials():
            result = self.store.register_current()
        self.assertEqual(result['account_ref'], w.account_ref('a'))
        self.assertEqual(len(self.store.index()['accounts']), 2)
        self.assertEqual(self.store.account('a')['device_profile'], self.a['device_profile'])
        self.assertEqual(self.store.account('a')['token']['refresh_token'], 'new-authorized-synthetic-refresh')

    def test_concurrent_account_edit_is_preserved_during_refresh(self):
        original = self.google.cloud
        def concurrent(operation, token, payload):
            result = original(operation, token, payload)
            if operation == 'fetchAvailableModels':
                account = self.store.account('a')
                account['external_edit'] = True
                self.write(self.store.account_path('a'), account)
            return result
        self.google.cloud = concurrent
        with self.assertRaisesRegex(w.WorkflowError, 'CHANGED_BEFORE_WRITE'):
            self.store.refresh_account('a')
        self.assertTrue(self.store.account('a')['external_edit'])

    def test_malformed_quota_never_commits_an_eligible_snapshot(self):
        original = self.google.cloud
        def invalid(operation, token, payload):
            if operation == 'fetchAvailableModels':
                return {'models': {'gemini-synthetic': {'quotaInfo': {'remainingFraction': True}}}}
            return original(operation, token, payload)
        self.google.cloud = invalid
        before = self.store.account_path('a').read_bytes()
        with self.assertRaises(w.WorkflowError):
            self.store.refresh_account('a')
        self.assertEqual(self.store.account_path('a').read_bytes(), before)

    def test_partial_refresh_reports_coverage_and_preserves_failed_account(self):
        original = self.store.refresh_account
        def partial(identifier):
            if identifier == 'b':
                raise w.WorkflowError('ACCOUNT_NETWORK_UNAVAILABLE')
            return original(identifier)
        with mock.patch.object(self.store, 'refresh_account', side_effect=partial):
            self.assertEqual(self.store.request('POST', '/api/accounts/refresh'), {'total': 2, 'success': 1, 'failed': 1})
        self.assertEqual(self.store.account('b')['quota']['last_updated'], 1)

    def test_register_current_reuses_binding_and_keeps_native_identity(self):
        with self.credentials():
            result = self.store.register_current()
        self.assertEqual(result['account_ref'], w.account_ref('a'))
        self.assertFalse(result['identity_changed'])
        self.assertEqual(self.current, self.payload(self.a))
        self.assertEqual(self.store.account('a')['device_profile'], self.a['device_profile'])
        self.assertEqual(len(self.store.index()['accounts']), 2)

    def test_first_enrollment_needs_no_tools_files(self):
        empty = n.NativeAccounts(self.root / 'fresh-install', w, transport=self.google, storage_paths=[])
        with self.credentials():
            result = empty.register_current()
        self.assertEqual(result['registered_accounts'], 1)
        self.assertEqual(len(empty.accounts()), 1)
        self.assertFalse((empty.directory / 'gui_config.json').exists())

    def test_unverified_userinfo_cannot_enroll_credentials(self):
        empty = n.NativeAccounts(self.root / 'fresh-install', w, transport=self.google, storage_paths=[])
        self.google.call = lambda *args, **kwargs: {'email': 'a@example.test', 'verified_email': False}
        with self.credentials(), self.assertRaisesRegex(w.WorkflowError, 'EMAIL_UNVERIFIED'):
            empty.register_current()
        self.assertFalse(empty.directory.exists())

    def test_switch_verifies_complete_credentials_and_preserves_metadata(self):
        with self.credentials() as (read, write):
            result = w.update_cli_target(self.store, w.account_ref('b'), refresh_token=False, reader=read, writer=write,
                                         credential_directory=self.store.credential_directory, storage_paths=[])
        self.assertEqual(result['account_backend'], 'builtin')
        self.assertEqual(result['switch_method'], 'builtin_native_account_core')
        self.assertEqual(self.current, self.payload(self.b))
        self.assertEqual(self.store.index()['unknown_future_metadata'], self.index['unknown_future_metadata'])
        self.assertEqual(self.store.account('b')['device_profile'], self.b['device_profile'])
        self.assertEqual(self.google.calls, [])

    def test_device_json_and_database_row_match_upstream_fields(self):
        storage = self.root / 'legacy/storage.json'
        self.write(storage, {'preserve_other_settings': True})
        database = storage.with_name('state.vscdb')
        with contextlib.closing(sqlite3.connect(database)) as connection, connection:
            connection.execute('CREATE TABLE ItemTable (key TEXT PRIMARY KEY,value TEXT)')
            connection.execute('INSERT INTO ItemTable VALUES (?,?)', ('storage.serviceMachineId', 'before'))
            connection.execute('INSERT INTO ItemTable VALUES (?,?)', ('unrelated', 'preserve'))
        self.store.storage_paths = [storage]
        with self.credentials() as (read, write):
            w.update_cli_target(self.store, w.account_ref('b'), refresh_token=False, reader=read, writer=write,
                                credential_directory=self.store.credential_directory, storage_paths=[storage])
        self.assertTrue(w.fingerprint_matches(w.read_json(storage), self.b['device_profile']))
        self.assertTrue(w.read_json(storage)['preserve_other_settings'])
        with contextlib.closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM ItemTable WHERE key='unrelated'").fetchone(), ('preserve',))

    def test_owned_partial_switch_restores_original_bytes_and_credential(self):
        paths = [self.store.directory / 'accounts.json', self.store.account_path('a'), self.store.account_path('b'),
            *self.store.credential_directory.rglob('*')]
        originals = {path: path.read_bytes() for path in paths if path.is_file()}
        original_commit = self.store.commit
        failed = False
        def fail_once(path, value, before):
            nonlocal failed
            if Path(path).name == 'oauth_creds.json' and not failed:
                failed = True
                raise OSError('synthetic write failure')
            return original_commit(path, value, before)
        with self.credentials() as (read, write), mock.patch.object(self.store, 'commit', side_effect=fail_once):
            with self.assertRaises(OSError):
                w.update_cli_target(self.store, w.account_ref('b'), refresh_token=False, reader=read, writer=write,
                                    credential_directory=self.store.credential_directory, storage_paths=[])
        self.assertEqual(self.current, self.payload(self.a))
        for path, value in originals.items():
            self.assertEqual(path.read_bytes(), value, str(path))

    def test_bundled_session_does_not_start_a_manager_process(self):
        with mock.patch.object(w.subprocess, 'Popen') as start:
            with w.manager_session(self.store, start=True, mode='desktop'):
                pass
            start.assert_not_called()

    def test_concurrent_file_edit_is_not_overwritten_by_switch_or_recovery(self):
        path = self.store.credential_directory / 'oauth_creds.json'
        original_commit = self.store.commit
        edited = False
        def external_edit(file, value, before):
            nonlocal edited
            if Path(file) == path and not edited:
                edited = True
                self.write(path, {'external_owner': 'preserve'})
            return original_commit(file, value, before)
        with self.credentials() as (read, write), mock.patch.object(self.store, 'commit', side_effect=external_edit):
            with self.assertRaisesRegex(w.WorkflowError, 'CHANGED_BEFORE_WRITE'):
                w.update_cli_target(self.store, w.account_ref('b'), refresh_token=False, reader=read, writer=write,
                                    credential_directory=self.store.credential_directory, storage_paths=[])
        self.assertEqual(w.read_json(path), {'external_owner': 'preserve'})
        self.assertEqual(self.current, self.payload(self.a))

    def test_new_failed_operation_cannot_roll_back_previous_completed_switch(self):
        with self.credentials() as (read, write):
            w.update_cli_target(self.store, w.account_ref('b'), refresh_token=False, reader=read, writer=write,
                                credential_directory=self.store.credential_directory, storage_paths=[])
            token = self.store.account('a')
            token['token']['expiry_timestamp'] = int(time.time()) + 60
            self.write(self.store.account_path('a'), token)
            self.google.failure = w.WorkflowError('ACCOUNT_NETWORK_UNAVAILABLE')
            with self.assertRaisesRegex(w.WorkflowError, 'NETWORK_UNAVAILABLE'):
                w.update_cli_target(self.store, w.account_ref('a'), refresh_token=False, reader=read, writer=write,
                                    credential_directory=self.store.credential_directory, storage_paths=[])
        self.assertEqual(self.current, self.payload(self.b))
        self.assertEqual(self.store.index()['current_account_id'], 'b')

    def test_per_account_proxy_binding_is_not_silently_ignored(self):
        account = self.store.account('a')
        account['proxy_id'] = 'configured-external-route'
        self.write(self.store.account_path('a'), account)
        with self.assertRaisesRegex(w.WorkflowError, 'BOUND_PROXY_REQUIRES_TOOLS'):
            self.store.refresh_account('a')
        self.assertEqual(self.google.calls, [])

    def test_default_builtin_diagnostics_do_not_discover_tools_executable(self):
        cli = self.root / 'synthetic.exe'
        cli.write_bytes(b'not executable; never launched')
        paths = SimpleNamespace(cli_path=lambda value: cli, settings_path=lambda value: self.root / 'missing-settings.json',
                                manager_directory=lambda value: Path(value))
        args = SimpleNamespace(account_backend='builtin', manager_dir=str(self.store.directory), manager_cli=None,
                               cli=None, settings_file=None, preflight_dir='synthetic')
        with mock.patch.object(w, 'sibling_module', side_effect=lambda name: paths if name == 'runtime_paths' else n), \
             mock.patch.object(w, 'require_receipt', return_value={}), \
             mock.patch.object(w, 'identify', return_value={'account_ref': w.account_ref('a')}), \
             mock.patch.object(w, 'worker_lock', side_effect=contextlib.nullcontext), \
             mock.patch.object(w, 'Manager') as external:
            result = w.doctor(args)
        self.assertEqual(result['status'], 'READY')
        self.assertFalse(result['checks']['account_backend']['tools_installation_required'])
        external.assert_not_called()

    @unittest.skipUnless(w.os.name == 'nt', 'Windows private-file ACL')
    def test_new_account_files_have_protected_non_null_dacl(self):
        security = w.sibling_module('file_security')
        observed = security._read_security(security._load_api(), self.store.account_path('a'))
        self.assertTrue(observed.protected)
        self.assertIsNotNone(observed.dacl)
        self.assertNotEqual(observed.dacl, ('null',))
        self.assertEqual(len(observed.dacl[1]), 2)

    def test_factory_can_load_builtin_without_importlib_module_registration(self):
        with mock.patch.object(w, 'Manager') as external:
            service = w.account_service(SimpleNamespace(account_backend='builtin', manager_dir=str(self.store.directory)))
            self.assertTrue(service.is_builtin)
            self.assertEqual(len(service.accounts()), 2)
            external.assert_not_called()

    def test_unknown_backend_never_falls_back_to_tools(self):
        with mock.patch.object(w, 'Manager') as external:
            with self.assertRaisesRegex(w.WorkflowError, 'INVALID_ACCOUNT_BACKEND'):
                w.account_service(SimpleNamespace(account_backend='typo', manager_dir=str(self.store.directory)))
            external.assert_not_called()

    def test_origin_guard_runs_before_a_network_request(self):
        transport = n.GoogleTransport(w)
        with mock.patch.object(transport.opener, 'open') as request:
            with self.assertRaisesRegex(w.WorkflowError, 'ORIGIN_REFUSED'):
                transport.call('https://example.test/api', token='synthetic-private')
            request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
