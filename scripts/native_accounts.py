"""Native account core adapted from Antigravity Tools (lbjlaq and contributors).

SPDX-License-Identifier: CC-BY-NC-SA-4.0
Source commit b601f5e7375343343922ccab5e50653bc3450c3b; see NOTICE.md.
Modified 2026-10-13: Python stdlib port, no desktop service, strict parsing,
owned writes, local enrollment and scoped Windows credential integration.
This module handles account operations only; inference stays in native agy.
"""
import copy
import contextlib
from datetime import datetime, timezone
import hmac
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import string
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

CLIENT_KEY = 'antigravity_enterprise'
# Public desktop OAuth application identifiers from the pinned upstream source.
# These are not an account's credentials or a privately issued application secret.
CLIENT_ID = '1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com'
CLIENT_SECRET = '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'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
USERINFO_URL = 'https://www.googleapis.com/oauth2/v2/userinfo'
CLOUD_HOSTS = ('daily-cloudcode-pa.googleapis.com',
               'daily-cloudcode-pa.sandbox.googleapis.com', 'cloudcode-pa.googleapis.com')


def new_profile():
    # Upstream's random_hex is lowercase alphanumeric, despite its name.
    return {'machine_id': 'auth0|user_' + ''.join(secrets.choice(string.ascii_letters + string.digits).lower() for _ in range(32)),
            'mac_machine_id': str(uuid.uuid4()), 'dev_device_id': str(uuid.uuid4()),
            'sqm_id': '{' + str(uuid.uuid4()).upper() + '}'}


def profile_from_storage(data):
    nested = data.get('telemetry', {})
    fields = {'machine_id': 'machineId', 'mac_machine_id': 'macMachineId',
              'dev_device_id': 'devDeviceId', 'sqm_id': 'sqmId'}
    result = {key: (nested.get(field) if isinstance(nested, dict) else None) or data.get('telemetry.' + field)
              for key, field in fields.items()}
    return result if all(isinstance(value, str) and value for value in result.values()) else None


class GoogleTransport:
    """Send account requests only to fixed Google HTTPS origins; refuse redirects."""
    def __init__(self, workflow):
        self.w = workflow
        environment, _ = workflow.cli_environment()
        proxy = next((environment[key] for key in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy', 'ALL_PROXY', 'all_proxy')
                      if environment.get(key)), None)
        if proxy and urllib.parse.urlsplit(proxy).scheme not in {'http', 'https'}:
            raise workflow.WorkflowError('ACCOUNT_PROXY_REQUIRES_HTTP_HTTPS')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({'https': proxy} if proxy else {}),
                                                  workflow.NoRedirect())
        self.user_agent = os.environ.get('AGY_SUBAGENT_ACCOUNT_USER_AGENT', 'vscode/1.X.X (Antigravity)')

    def call(self, url, token=None, body=None, form=None):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname not in {*CLOUD_HOSTS, 'oauth2.googleapis.com', 'www.googleapis.com'}:
            raise self.w.WorkflowError('ACCOUNT_REQUEST_ORIGIN_REFUSED')
        headers = {'User-Agent': self.user_agent, 'Accept': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        raw = None
        if form is not None:
            raw = urllib.parse.urlencode(form).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        elif body is not None:
            raw = self.w.json_bytes(body)
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(url, data=raw, headers=headers)
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise self.w.WorkflowError('ACCOUNT_RESPONSE_TOO_LARGE')
                result = self.w.strict_json(raw.decode('utf-8-sig'))
                if not isinstance(result, dict):
                    raise self.w.WorkflowError('ACCOUNT_RESPONSE_INVALID')
                return result
        except urllib.error.HTTPError as error:
            # Never include an upstream body or the request credentials in diagnostics.
            code = None
            if url == TOKEN_URL:
                try:
                    diagnostic = self.w.strict_json(error.read(8192).decode('utf-8'))
                    if isinstance(diagnostic, dict) and diagnostic.get('error') in {'invalid_grant', 'invalid_client', 'unauthorized_client'}:
                        code = diagnostic['error']
                except (ValueError, UnicodeError, self.w.WorkflowError):
                    pass
            raise self.w.WorkflowError('ACCOUNT_HTTP_' + str(error.code) + (':' + code if code else '')) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            raise self.w.WorkflowError('ACCOUNT_NETWORK_UNAVAILABLE') from None
        except UnicodeError:
            raise self.w.WorkflowError('ACCOUNT_RESPONSE_INVALID') from None

    def cloud(self, operation, token, payload):
        for index, host in enumerate(CLOUD_HOSTS):
            try:
                return self.call('https://' + host + '/v1internal:' + operation, token=token, body=payload)
            except self.w.WorkflowError as error:
                message = str(error)
                retryable = message == 'ACCOUNT_NETWORK_UNAVAILABLE' or message.startswith('ACCOUNT_HTTP_429') or re.match(r'ACCOUNT_HTTP_5\d\d', message)
                if not retryable or index == len(CLOUD_HOSTS) - 1:
                    raise


class NativeAccounts:
    """Local upstream-compatible account inventory, with no manager HTTP service."""
    is_builtin = True
    backend = 'builtin'

    def __init__(self, directory, workflow, *, transport=None, credential_directory=None, storage_paths=None):
        self.w = workflow
        self.directory = workflow.sibling_module('runtime_paths').manager_directory(directory)
        self.transport = transport
        self.credential_directory = Path(credential_directory or Path.home() / '.gemini')
        self.storage_paths = storage_paths
        self.inventory_source = 'local_cache'

    def google(self):
        if self.transport is None:
            self.transport = GoogleTransport(self.w)
        return self.transport

    def index(self):
        path = self.directory / 'accounts.json'
        if not path.is_file():
            raise self.w.WorkflowError('ACCOUNT_STORE_EMPTY: run register-current after native CLI sign-in')
        result = self.w.read_json(path)
        if not isinstance(result, dict) or not isinstance(result.get('accounts'), list):
            raise self.w.WorkflowError('LOCAL_ACCOUNT_INDEX_INVALID')
        identifiers = [item.get('id') if isinstance(item, dict) else None for item in result['accounts']]
        if any(not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', value) for value in identifiers) or len(set(identifiers)) != len(identifiers):
            raise self.w.WorkflowError('MANAGER_AMBIGUOUS_INVENTORY')
        return result

    def account_path(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', identifier):
            raise self.w.WorkflowError('INVALID_LOCAL_ACCOUNT_ID')
        return self.directory / 'accounts' / (identifier + '.json')

    def account(self, identifier):
        result = self.w.read_json(self.account_path(identifier))
        if not isinstance(result, dict) or result.get('id') != identifier or not isinstance(result.get('token'), dict):
            raise self.w.WorkflowError('LOCAL_ACCOUNT_ID_MISMATCH')
        if not isinstance(result.get('email'), str) or '@' not in result['email']:
            raise self.w.WorkflowError('ACCOUNT_EMAIL_INVALID')
        return result

    def check_network_binding(self, account):
        if account.get('proxy_id'):
            raise self.w.WorkflowError('ACCOUNT_BOUND_PROXY_REQUIRES_TOOLS_BACKEND: preserve configured per-account route')

    def accounts(self, *, allow_local=False):
        index = self.index()
        if not index['accounts']:
            raise self.w.WorkflowError('ACCOUNT_STORE_EMPTY: run register-current after native CLI sign-in')
        defaults = {'disabled': False, 'proxy_disabled': False, 'validation_blocked': False,
                    'protected_models': [], 'live_limited_models': {}}
        fields = ('id', 'email', 'disabled', 'proxy_disabled', 'validation_blocked', 'validation_blocked_until',
                  'protected_models', 'live_limited_models', 'quota')
        result = []
        for summary in index['accounts']:
            account = self.account(summary['id'])
            result.append({**{key: account.get(key, defaults.get(key)) for key in fields},
                           'is_current': account['id'] == index.get('current_account_id')})
        return result

    def raw(self, path):
        path = Path(path)
        if path.is_symlink():
            raise self.w.WorkflowError('ACCOUNT_STATE_SYMLINK_REFUSED')
        return path.read_bytes() if path.is_file() else None

    def commit(self, path, value, before):
        if self.raw(path) != before:
            raise self.w.WorkflowError('ACCOUNT_STATE_CHANGED_BEFORE_WRITE')
        self.w.atomic_write(path, self.w.json_bytes(value), private=True)
        if getattr(self, '_switch_tracking', False):
            self._switch_files[Path(path)] = (before, self.w.json_bytes(value))
        if self.w.read_json(path) != value:
            raise self.w.WorkflowError('ACCOUNT_STATE_READBACK_FAILED')

    def fresh_token(self, token):
        token = copy.deepcopy(token)
        if not isinstance(token.get('access_token'), str) or not token['access_token']:
            raise self.w.WorkflowError('ACCOUNT_TOKEN_INVALID_ACCESS_TOKEN')
        if not isinstance(token.get('refresh_token'), str) or not token['refresh_token']:
            raise self.w.WorkflowError('ACCOUNT_TOKEN_INVALID_REFRESH_TOKEN')
        for key in ('id_token', 'oauth_client_key'):
            if token.get(key) is not None and (not isinstance(token[key], str) or not token[key]):
                raise self.w.WorkflowError('ACCOUNT_TOKEN_INVALID_' + key.upper())
        expiry = token.get('expiry_timestamp')
        if not self.w.number(expiry, 0, 1e15):
            raise self.w.WorkflowError('ACCOUNT_TOKEN_INVALID_EXPIRY')
        seconds = expiry / 1000 if expiry > 10_000_000_000 else expiry
        if seconds > time.time() + 900:
            return token
        clients = {CLIENT_KEY: (CLIENT_ID, CLIENT_SECRET)}
        for entry in os.environ.get('ANTIGRAVITY_OAUTH_CLIENTS', '').split(';'):
            parts = [part.strip() for part in entry.split('|')]
            if len(parts) >= 3 and all(parts[:3]):
                clients[parts[0].lower()] = (parts[1], parts[2])
        preferred = token.get('oauth_client_key')
        if preferred is not None and not isinstance(preferred, str):
            raise self.w.WorkflowError('OAUTH_CLIENT_KEY_INVALID')
        active = os.environ.get('ANTIGRAVITY_OAUTH_CLIENT_KEY', CLIENT_KEY).lower()
        candidates = list(dict.fromkeys([key for key in (preferred, active, *clients) if key in clients]))
        for index, key in enumerate(candidates):
            client_id, client_secret = clients[key]
            try:
                result = self.google().call(TOKEN_URL, form={'client_id': client_id, 'client_secret': client_secret,
                    'grant_type': 'refresh_token', 'refresh_token': token['refresh_token']})
            except self.w.WorkflowError as error:
                if index + 1 < len(candidates) and re.match(r'ACCOUNT_HTTP_(400|401|403)', str(error)):
                    continue
                raise
            if not isinstance(result.get('access_token'), str) or not result['access_token'] or not self.w.number(result.get('expires_in'), 1, 604800):
                raise self.w.WorkflowError('OAUTH_REFRESH_RESPONSE_INVALID')
            token.update(access_token=result['access_token'], expires_in=result['expires_in'],
                         expiry_timestamp=int(time.time()) + result['expires_in'], token_type='Bearer')
            if result.get('id_token') is not None:
                if not isinstance(result['id_token'], str) or not result['id_token']:
                    raise self.w.WorkflowError('OAUTH_REFRESH_RESPONSE_INVALID')
                token['id_token'] = result['id_token']
            # Match upstream's legacy-account rule; do not accidentally enterprise-lock it.
            if preferred is not None or token.get('project_id') or key != CLIENT_KEY:
                token['oauth_client_key'] = key
            return token
        raise self.w.WorkflowError('OAUTH_CLIENT_UNAVAILABLE')

    def refresh_account(self, identifier):
        path = self.account_path(identifier)
        before = self.raw(path)
        account = self.account(identifier)
        self.check_network_binding(account)
        if account.get('disabled', False) is not False or account.get('validation_blocked', False) is not False:
            raise self.w.WorkflowError('ACCOUNT_DISABLED_OR_BLOCKED')
        account['token'] = self.fresh_token(account['token'])
        token = account['token']
        project = token.get('project_id')
        tier = (account.get('quota') or {}).get('subscription_tier')
        try:
            info = self.google().cloud('loadCodeAssist', token['access_token'], {'metadata': {'ideType': 'ANTIGRAVITY'}})
            fresh_project = info.get('cloudaicompanionProject')
            if isinstance(fresh_project, str) and fresh_project.strip():
                project = fresh_project
                token['project_id'] = project
            selected = info.get('paidTier') or info.get('currentTier') or {}
            raw_tier = str(selected.get('id') or selected.get('name') or 'free-tier').lower()
            tier = 'ULTRA' if 'ultra' in raw_tier else ('PRO' if 'pro' in raw_tier else 'FREE')
        except self.w.WorkflowError:
            pass  # Upstream preserves prior project/tier when this informational call fails.
        payload = {'project': project} if project else {}
        try:
            response = self.google().cloud('fetchAvailableModels', token['access_token'], payload)
        except self.w.WorkflowError as error:
            if str(error) != 'ACCOUNT_HTTP_403':
                raise
            response = {'models': {}}
            forbidden = True
        else:
            forbidden = False
        models = response.get('models')
        if not isinstance(models, dict):
            raise self.w.WorkflowError('QUOTA_RESPONSE_INVALID')
        rows = []
        for name, info in models.items():
            if not isinstance(info, dict):
                raise self.w.WorkflowError('QUOTA_RESPONSE_INVALID')
            quota = info.get('quotaInfo')
            if quota is None:
                continue
            if not isinstance(quota, dict) or (quota.get('remainingFraction') is not None and not self.w.number(quota.get('remainingFraction'), 0, 1)):
                raise self.w.WorkflowError('QUOTA_RESPONSE_INVALID')
            rows.append({'name': name, 'percentage': quota['remainingFraction'] * 100 if quota.get('remainingFraction') is not None else None,
                         'reset_time': quota.get('resetTime', ''), 'model': info.get('model')})
        groups = None
        if not forbidden:
            try:
                summary = self.google().cloud('retrieveUserQuotaSummary', token['access_token'], payload)
                if not isinstance(summary.get('groups'), list):
                    raise self.w.WorkflowError('QUOTA_SUMMARY_INVALID')
                groups = []
                for group in summary['groups']:
                    if not isinstance(group, dict) or not isinstance(group.get('buckets'), list):
                        raise self.w.WorkflowError('QUOTA_SUMMARY_INVALID')
                    buckets = []
                    for bucket in group['buckets']:
                        if not isinstance(bucket, dict) or not self.w.number(bucket.get('remainingFraction'), 0, 1):
                            raise self.w.WorkflowError('QUOTA_SUMMARY_INVALID')
                        buckets.append({'bucket_id': bucket.get('bucketId', ''), 'window': bucket.get('window', ''),
                            'remaining_fraction': bucket['remainingFraction'], 'reset_time': bucket.get('resetTime', ''),
                            'observed_at': int(time.time() * 1000)})
                    groups.append({'display_name': group.get('displayName', ''), 'buckets': buckets})
            except self.w.WorkflowError:
                groups = None
        quota = copy.deepcopy(account.get('quota') or {})
        quota.update(models=rows, quota_groups=groups, last_updated=int(time.time()), is_forbidden=forbidden,
                     subscription_tier=tier, source='builtin_google_account_api')
        account['quota'] = quota
        self.commit(path, account, before)
        return quota

    def request(self, method, route):
        # Interface compatibility with the supervisor; these strings never become HTTP requests.
        match = re.fullmatch(r'/api/accounts/([A-Za-z0-9_-]+)/quota', route)
        if method == 'GET' and match:
            return self.refresh_account(match[1])
        if (method, route) == ('GET', '/api/accounts'):
            return {'accounts': self.accounts()}
        if (method, route) != ('POST', '/api/accounts/refresh'):
            raise self.w.WorkflowError('MANAGER_ROUTE_NOT_ALLOWED')
        rows = [row for row in self.accounts() if (row.get('quota') or {}).get('is_forbidden') is not True]
        stats = {'total': len(rows), 'success': 0, 'failed': 0}
        for row in rows:
            try:
                self.refresh_account(row['id'])
                stats['success'] += 1
            except (self.w.WorkflowError, OSError, ValueError):
                stats['failed'] += 1
        return stats

    def register_current(self):
        payload = self.w.read_cli_credential_payload()
        native = payload['token']
        index_path = self.directory / 'accounts.json'
        index_before = self.raw(index_path)
        index = self.index() if index_before is not None else {'version': '2.0', 'accounts': [], 'current_account_id': None, 'current_target_ide': None}
        matches = [summary['id'] for summary in index['accounts']
                   if hmac.compare_digest(str(self.account(summary['id'])['token'].get('refresh_token', '')).encode(), native['refresh_token'].encode())]
        if len(matches) > 1:
            raise self.w.WorkflowError('CLI_IDENTITY_UNKNOWN_OR_AMBIGUOUS')
        existing = self.account(matches[0]) if matches else None
        if existing:
            self.check_network_binding(existing)
        try:
            expiry = datetime.fromisoformat(native['expiry'].replace('Z', '+00:00')).timestamp()
        except (KeyError, TypeError, ValueError):
            raise self.w.WorkflowError('CLI_CREDENTIAL_INVALID_EXPIRY') from None
        token = self.fresh_token({**(existing['token'] if existing else {}), 'access_token': native.get('access_token'), 'refresh_token': native.get('refresh_token'),
            'token_type': 'Bearer', 'expiry_timestamp': expiry, 'expires_in': max(0, int(expiry - time.time())),
            **({'id_token': payload['id_token']} if payload.get('id_token') else {})})
        info = self.google().call(USERINFO_URL, token=token['access_token'])
        email = info.get('email')
        if not isinstance(email, str) or '@' not in email or info.get('verified_email') is not True:
            raise self.w.WorkflowError('CLI_ACCOUNT_EMAIL_UNVERIFIED')
        same_email = [summary for summary in index['accounts'] if str(summary.get('email', '')).lower() == email.lower()]
        if not matches and same_email:
            if len(same_email) != 1:
                raise self.w.WorkflowError('ACCOUNT_EMAIL_AMBIGUOUS: inspect duplicate account records')
            # Explicit enrollment of a verified native login also supports reauthorization.
            matches = [same_email[0]['id']]
        identifier = matches[0] if matches else str(uuid.uuid4())
        path = self.account_path(identifier)
        before = self.raw(path)
        account = self.account(identifier) if before is not None else {'id': identifier, 'email': email, 'name': info.get('name'),
            'priority': 50, 'created_at': int(time.time()), 'last_used': int(time.time()), 'disabled': False,
            'proxy_disabled': False, 'validation_blocked': False, 'protected_models': [], 'live_limited_models': {}, 'quota': None}
        if account.get('email', '').lower() != email.lower():
            raise self.w.WorkflowError('CLI_ACCOUNT_EMAIL_MISMATCH')
        token['email'] = email
        # Existing account metadata, device binding and client/project fields remain owned by that account.
        account['token'] = {**account.get('token', {}), **token}
        if not account.get('device_profile'):
            storages = self.storage_paths if self.storage_paths is not None else self.w.fingerprint_storage_paths(self)
            account['device_profile'] = (profile_from_storage(self.w.read_json(storages[0])) if storages else None) or new_profile()
        self.commit(path, account, before)
        if not matches:
            index['accounts'].append({key: account.get(key) for key in ('id', 'email', 'name', 'created_at', 'last_used', 'disabled', 'proxy_disabled')})
        index.update(current_account_id=identifier, current_target_ide='agy')
        self.commit(index_path, index, index_before)
        return {'status': 'REGISTERED', 'account_ref': self.w.account_ref(identifier), 'account_backend': 'builtin',
                'registered_accounts': len(index['accounts']), 'credentials_source': 'native_windows_store', 'identity_changed': False}

    def switch_cli(self, identifier):
        self._switch_tracking = False
        self._switch_files = {}
        self._switch_databases = {}
        self._switch_credential_written = None
        index_path = self.directory / 'accounts.json'
        index_before = self.raw(index_path)
        index = self.index()
        if identifier not in {row['id'] for row in index['accounts']}:
            raise self.w.WorkflowError('ACCOUNT_REF_UNKNOWN_OR_AMBIGUOUS')
        path = self.account_path(identifier)
        before = self.raw(path)
        account = self.account(identifier)
        self.check_network_binding(account)
        if account.get('disabled', False) is not False or account.get('validation_blocked', False) is not False:
            raise self.w.WorkflowError('ACCOUNT_DISABLED_OR_BLOCKED')
        account['token'] = self.fresh_token(account['token'])
        if (account['token'].get('oauth_client_key') or '').strip().lower() == CLIENT_KEY and not account['token'].get('project_id'):
            try:
                project = self.google().cloud('loadCodeAssist', account['token']['access_token'], {'metadata': {'ideType': 'ANTIGRAVITY'}}).get('cloudaicompanionProject')
                if isinstance(project, str) and project.strip():
                    account['token']['project_id'] = project
            except self.w.WorkflowError:
                pass  # Same best-effort project readiness as upstream.
        if account.get('device_profile') is None:
            account['device_profile'] = new_profile()
            account.setdefault('device_history', []).append({'id': str(uuid.uuid4()), 'created_at': int(time.time()),
                'label': 'auto_generated', 'profile': account['device_profile'], 'is_current': True})
        profile = account['device_profile']
        if not isinstance(profile, dict) or any(not isinstance(profile.get(key), str) or not profile[key]
                for key in ('machine_id', 'mac_machine_id', 'dev_device_id', 'sqm_id')):
            raise self.w.WorkflowError('NATIVE_SWITCH_BOUND_FINGERPRINT_MISSING')
        payload = self.w.build_cli_payload(account)
        if account['token'].get('id_token'):
            payload['id_token'] = account['token']['id_token']
        token = account['token']
        expiry = token['expiry_timestamp']
        fallback = {'access_token': token['access_token'], 'refresh_token': token['refresh_token'], 'token_type': 'Bearer',
            'expiry_date': expiry if expiry > 10_000_000_000 else expiry * 1000,
            'scope': 'https://www.googleapis.com/auth/userinfo.email openid https://www.googleapis.com/auth/cloud-platform https://www.googleapis.com/auth/userinfo.profile'}
        if token.get('id_token'):
            fallback['id_token'] = token['id_token']
        file_values = ((self.credential_directory / 'antigravity-cli/antigravity-oauth-token', payload),
                       (self.credential_directory / 'oauth_creds.json', fallback),
                       (self.credential_directory / 'google_accounts.json', {'active': account['email'], 'old': []}))
        storages = self.storage_paths if self.storage_paths is not None else self.w.fingerprint_storage_paths(self)
        if len(storages) > 1:
            raise self.w.WorkflowError('FINGERPRINT_LOCATION_AMBIGUOUS')
        file_before = {file: self.raw(file) for file, _ in file_values}
        storage_before = {Path(storage): self.raw(storage) for storage in storages}
        database_before = {Path(storage).with_name('state.vscdb'): self.w.fingerprint_database_value(Path(storage).with_name('state.vscdb'))
                           for storage in storages if Path(storage).with_name('state.vscdb').is_file()}
        credential_before = self.w.read_cli_credential_payload()
        self.w.identify(self, reader=lambda: credential_before['token']['refresh_token'])
        if self.raw(index_path) != index_before or self.raw(path) != before:
            raise self.w.WorkflowError('ACCOUNT_STATE_CHANGED_BEFORE_SWITCH')
        self._switch_tracking = True
        self._switch_files = {}
        self._switch_databases = {}
        self._switch_credential_before = credential_before
        self._switch_credential_written = None
        self.commit(path, account, before)
        if self.w.read_cli_credential_payload() != credential_before:
            raise self.w.WorkflowError('ACCOUNT_STATE_CHANGED_BEFORE_SWITCH')
        self.w.write_cli_credential_payload(payload)
        self._switch_credential_written = copy.deepcopy(payload)
        if not self.w.native_credential_matches(self.w.read_cli_credential_payload(), account):
            raise self.w.WorkflowError('SWITCH_CREDENTIAL_READBACK_FAILED')
        for file, value in file_values:
            self.commit(file, value, file_before[file])
        for storage in storages:
            raw = storage_before[Path(storage)]
            data = self.w.read_json(storage)
            if not isinstance(data, dict):
                raise self.w.WorkflowError('FINGERPRINT_STORAGE_INVALID')
            fields = {'machineId': 'machine_id', 'macMachineId': 'mac_machine_id', 'devDeviceId': 'dev_device_id', 'sqmId': 'sqm_id'}
            data['telemetry'] = {**(data.get('telemetry') if isinstance(data.get('telemetry'), dict) else {}),
                                 **{key: profile[value] for key, value in fields.items()}}
            data.update({'telemetry.' + key: profile[value] for key, value in fields.items()})
            data['storage.serviceMachineId'] = profile['dev_device_id']
            self.commit(storage, data, raw)
            database = Path(storage).with_name('state.vscdb')
            if database.is_file():
                with contextlib.closing(sqlite3.connect(database, timeout=5)) as connection, connection:
                    connection.execute('BEGIN IMMEDIATE')
                    row = connection.execute("SELECT value FROM ItemTable WHERE key = 'storage.serviceMachineId'").fetchone()
                    if row != database_before[database]:
                        raise self.w.WorkflowError('ACCOUNT_DATABASE_CHANGED_BEFORE_WRITE')
                    connection.execute("INSERT OR REPLACE INTO ItemTable (key,value) VALUES ('storage.serviceMachineId',?)", (profile['dev_device_id'],))
                self._switch_databases[database] = (database_before[database], (profile['dev_device_id'],))
        index.update(current_account_id=identifier, current_target_ide='agy')
        self.commit(index_path, index, index_before)
        self._switch_tracking = False
        return {'switch_method': 'builtin_native_account_core', 'account_backend': 'builtin'}

    def restore_owned_switch(self):
        """Restore only bytes/credential/row still matching this instance's writes."""
        self._switch_tracking = False
        failures = []
        written = getattr(self, '_switch_credential_written', None)
        if written is not None:
            try:
                if self.w.read_cli_credential_payload() != written:
                    failures.append('concurrent_credential_edit_preserved')
                else:
                    self.w.write_cli_credential_payload(self._switch_credential_before)
                    if self.w.read_cli_credential_payload() != self._switch_credential_before:
                        failures.append('credential_restore_readback')
            except (self.w.WorkflowError, OSError, ValueError):
                failures.append('credential_restore_failed')
        for path, (before, after) in reversed(list(getattr(self, '_switch_files', {}).items())):
            try:
                if self.raw(path) != after:
                    failures.append('concurrent_file_edit_preserved')
                elif before is None:
                    failures.append('new_file_preserved')
                else:
                    self.w.atomic_write(path, before, private=True)
                    if self.raw(path) != before:
                        failures.append('file_restore_readback')
            except (self.w.WorkflowError, OSError, ValueError):
                failures.append('file_restore_failed')
        for path, (before, after) in getattr(self, '_switch_databases', {}).items():
            try:
                with contextlib.closing(sqlite3.connect(path, timeout=5)) as connection, connection:
                    connection.execute('BEGIN IMMEDIATE')
                    current = connection.execute("SELECT value FROM ItemTable WHERE key='storage.serviceMachineId'").fetchone()
                    if current != after:
                        failures.append('concurrent_database_edit_preserved')
                    elif before is None:
                        connection.execute("DELETE FROM ItemTable WHERE key='storage.serviceMachineId'")
                    else:
                        connection.execute("INSERT OR REPLACE INTO ItemTable (key,value) VALUES ('storage.serviceMachineId',?)", before)
            except sqlite3.Error:
                failures.append('database_restore_failed')
        return failures
