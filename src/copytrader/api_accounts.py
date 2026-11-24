"""Local ForeGate profile selection and account-scoped workspace lifetime."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

from .api_workspace import ApiWorkspace, connection_settings, object_data
from .foregate_api import ApiError, ForeGateAPI, user_id


class ApiAccounts:
    def __init__(self, service):
        self.service = service
        self.active = None
        self.context = None
        self.switching = False
        self.inflight = 0
        self.idle = asyncio.Event()
        self.idle.set()
        self.folder = service.root / 'foregate-accounts'
        self.folder.mkdir(mode=0o700, exist_ok=True)
        path = self.folder / 'profiles.db'
        self.db = sqlite3.connect(path, isolation_level=None)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS profiles (
                uid TEXT PRIMARY KEY, slot TEXT NOT NULL UNIQUE, label TEXT NOT NULL,
                registered_id TEXT NOT NULL, registration TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS local (name TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        self.db.execute("INSERT OR IGNORE INTO local VALUES ('draft', ?)", (self.new_reference(),))

    @staticmethod
    def new_reference():
        return 'fg_' + uuid.uuid4().hex

    def local(self, name):
        row = self.db.execute('SELECT value FROM local WHERE name=?', (name,)).fetchone()
        return row['value'] if row else None

    @property
    def armed_at(self):
        return self.active.armed_at if self.active else None

    def pause(self, reason):
        if self.active:
            self.active.pause(reason)

    def ingest(self, record, source, signal):
        return self.active.ingest(record, source, signal) if self.active else []

    def settings_view(self):
        return connection_settings(self.service)

    def status(self):
        data = self.active.status() if self.active else {'account_id': None, 'verified': False, 'armed': False}
        data['account_context'] = self.context
        return data

    def catalog(self):
        return {**self.status(), 'profiles': [dict(row) for row in self.db.execute(
                'SELECT uid,slot,label FROM profiles ORDER BY label COLLATE NOCASE,uid')],
                'guide_seen': self.local('guide_seen') == '1',
                'last_uid': self.local('last_uid'), 'draft_reference': self.local('draft'),
                'has_legacy_journal': (self.service.root / 'foregate-api.db').is_file()}

    async def dispatch(self, action, data):
        if action == 'guide_dismiss':
            self.db.execute("INSERT OR REPLACE INTO local VALUES ('guide_seen', '1')")
            return {}
        if action == 'account_catalog':
            return self.catalog()
        if action == 'status':
            return self.status()
        if action == 'settings':
            return self.settings_view()
        if self.switching:
            raise ApiError('正在切换账户，请等待连接完成。')
        if action in {'account_enroll', 'account_select'}:
            self.switching = True
            try:
                return await self.change(action, data)
            finally:
                self.switching = False
        if not self.active or data.get('account_context') != self.context:
            raise ApiError('账户已切换或未连接，请重新打开操作页面。')
        self.inflight += 1
        self.idle.clear()
        try:
            return await self.active.dispatch(action, data)
        finally:
            self.inflight -= 1
            if self.inflight == 0:
                self.idle.set()

    async def change(self, action, data):
        slot = data.get('slot')
        if not isinstance(slot, str) or not re.fullmatch(r'[0-9a-f]{32}', slot):
            raise ApiError('本机账户凭证标识无效。')
        credentials = data.get('credentials')
        enrolling = action == 'account_enroll'
        if enrolling:
            client, label = await self.enroll(data, credentials)
        else:
            row = self.db.execute('SELECT * FROM profiles WHERE slot=?', (slot,)).fetchone()
            if not row:
                raise ApiError('没有找到这个本机账户。')
            client = ForeGateAPI(credentials, {'profile_user_id': row['uid'], 'user_id': row['registered_id'],
                                               'registration': json.loads(row['registration'])})
            label = row['label']
        assets = object_data(await asyncio.to_thread(client.call, '/account/assets'))
        if str(assets.get('userId')) not in client.known_ids:
            raise ApiError('ForeGate 返回的资产账户与当前连接身份不一致。')

        previous = self.active
        if previous:
            previous.pause('正在切换 ForeGate 账户，自动跟单已暂停。')
            await self.idle.wait()
            # A request that was already running can leave listening on. Stop it after it drains.
            await self.service.pause()

        uid = client.account_id
        former = self.db.execute('SELECT slot FROM profiles WHERE uid=?', (uid,)).fetchone() if enrolling else None
        path = self.folder / (hashlib.sha256(uid.encode()).hexdigest() + '.db')
        same_workspace = previous and previous.account_id == uid
        if enrolling and data.get('mode') == 'existing' and not path.exists():
            self.migrate_legacy(path, client)
        if same_workspace:
            async with previous.operation_lock:
                self.save(client, slot, label, enrolling)
                previous.api = client
                previous.pending.clear()
                previous.mfa = None
                previous.totp_started = False
            workspace = previous
        else:
            workspace = ApiWorkspace(self.service, client, path)
            try:
                if previous:
                    await previous.close()
                    self.active, self.context = None, None
                self.save(client, slot, label, enrolling)
            except BaseException:
                workspace.db.close()
                raise
        workspace.verified = True
        self.active, self.context = workspace, uuid.uuid4().hex
        self.service.error = ''
        workspace.start_tracker()
        return {'account_id': uid, 'account_context': self.context, 'verified': True, 'assets': assets, 'label': label,
                'retired_slot': former['slot'] if former else None}

    async def enroll(self, data, credentials):
        label = data.get('label', '').strip() if isinstance(data.get('label'), str) else ''
        if len(label) > 50:
            raise ApiError('账户昵称最多 50 个字符。')
        mode = data.get('mode')
        if mode == 'new':
            expected = None
            reference = self.local('draft')
            if data.get('reference') != reference:
                raise ApiError('注册标识已更新，请重新打开注册表单。')
        elif mode == 'existing':
            expected = user_id(data.get('uid'))
            reference = data.get('reference')
        else:
            raise ApiError('请选择连接已有账户或创建新账户。')
        if not isinstance(reference, str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,128}', reference):
            raise ApiError('请填写有效的 marchantUserId。')
        registration = {'marchantUserId': reference}
        if (merchant := data.get('merchant_id')):
            if not isinstance(merchant, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', merchant):
                raise ApiError('请填写有效的 Merchant ID。')
            registration['merchantId'] = merchant
        client = await asyncio.to_thread(ForeGateAPI.enroll, credentials, registration, expected)
        return client, label or f'账户 {client.account_id}'

    def save(self, client, slot, label, enrolling):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if enrolling:
                self.db.execute('''INSERT INTO profiles VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(uid) DO UPDATE SET slot=excluded.slot, label=excluded.label,
                    registered_id=excluded.registered_id, registration=excluded.registration''',
                    (client.account_id, slot, label, client.registered_id, json.dumps(client.registration)))
                if client.registration['marchantUserId'] == self.local('draft'):
                    self.db.execute("UPDATE local SET value=? WHERE name='draft'", (self.new_reference(),))
            self.db.execute("INSERT OR REPLACE INTO local VALUES ('last_uid', ?)", (client.account_id,))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def migrate_legacy(self, target, client):
        source = self.service.root / 'foregate-api.db'
        if not source.is_file():
            return
        # The old single-account journal didn't record an owner. Adopt it only when the
        # old installation's separate binding file agrees with both verified identifiers.
        default = Path.home() / 'Desktop/Foregate/产品设计/OpenApi/nba_bot/.local/foregate-account.json'
        binding = Path(os.environ.get('COPYTRADER_FOREGATE_ACCOUNT', default)).expanduser()
        try:
            old = json.loads(binding.read_text())
            if str(old.get('profile_user_id')) != client.account_id or (
                    old.get('registration', {}).get('marchantUserId') != client.registration['marchantUserId']):
                return
        except (OSError, ValueError, AttributeError):
            return
        staging = target.with_suffix('.tmp')
        original = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)
        copy = sqlite3.connect(staging)
        try:
            os.chmod(staging, 0o600)
            original.backup(copy)
        finally:
            copy.close()
            original.close()
        os.replace(staging, target)

    async def close(self):
        if self.active:
            await self.active.close()
        self.db.close()
