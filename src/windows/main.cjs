'use strict';

const { app, BrowserWindow, clipboard, dialog, ipcMain, protocol, safeStorage, shell } = require('electron');
const { randomUUID } = require('node:crypto');
const { mkdir, readFile, rename, rm, writeFile } = require('node:fs/promises');
const { join } = require('node:path');
const { Backend, SERVICE_ERROR } = require('./backend.cjs');

const PAGE = 'workspace://local/api.html';
const assets = new Map([
  [PAGE, ['api.html', 'text/html']],
  ['workspace://local/api.css', ['api.css', 'text/css']],
  ['workspace://local/api.js', ['api.js', 'text/javascript']]
]);
const externalHosts = new Set(['docs.forepaas.org', 'my.telegram.org']);
const allowed = new Set([
  'guide_dismiss', 'account_catalog', 'account_enroll', 'account_select', 'book', 'config', 'connect', 'cycle_activity',
  'deposit_address', 'detail', 'execute', 'follow_arm', 'follow_listen', 'follow_pause', 'market_categories',
  'markets', 'mfa_begin', 'mfa_email', 'mfa_setup', 'mfa_setup_complete', 'mfa_status', 'mfa_verify',
  'open_orders', 'positions', 'prepare', 'query_orders', 'reconcile', 'review_unknown', 'sell_positions',
  'settings', 'status', 'trades', 'withdraw_limit'
].map(action => 'api_' + action).concat(['copy_address', 'save_settings', 'tg_send_code', 'tg_sign_in']));
const confirmations = new Set(['api_execute', 'api_follow_arm', 'api_mfa_setup', 'api_review_unknown']);
const credentialKeys = ['appKey', 'appSecret', 'apiKey'];

class UserError extends Error {}

class CredentialStore {
  constructor(folder) {
    this.folder = folder;
  }

  async read(name) {
    return safeStorage.decryptString(await readFile(join(this.folder, name)));
  }

  async write(name, value) {
    if (!safeStorage.isEncryptionAvailable()) throw new Error('Encryption unavailable');
    const encrypted = safeStorage.encryptString(value);
    await mkdir(this.folder, { recursive:true });
    const temp = join(this.folder, randomUUID() + '.tmp');
    try {
      await writeFile(temp, encrypted, { flag:'wx' });
      await rename(temp, join(this.folder, name));
    } finally {
      await rm(temp, { force:true }).catch(() => {});
    }
  }

  remove(name) {
    return rm(join(this.folder, name), { force:true }).catch(() => {});
  }
}

class Workspace {
  constructor(window, backend, credentials) {
    this.window = window;
    this.backend = backend;
    this.credentials = credentials;
    this.prepared = new Map();
    this.switching = false;
    this.confirming = false;
  }

  async changeAccount(action, data) {
    if (this.switching || this.confirming) throw new UserError('请先完成当前账户操作。');
    this.switching = true;
    try {
      const enrolling = action === 'api_account_enroll';
      const slot = enrolling ? randomUUID().replaceAll('-', '') : data.slot;
      if (typeof slot !== 'string' || !/^[0-9a-f]{32}$/.test(slot)) throw new UserError('本机账户标识无效。');
      const name = 'foregate-' + slot;
      let credentials;
      if (enrolling) {
        credentials = {};
        for (const key of credentialKeys) {
          const value = typeof data[key] === 'string' ? data[key].trim() : '';
          delete data[key];
          if (!/^[!-~]{1,1024}$/.test(value)) throw new UserError('请填写有效的 ForeGate OpenAPI 凭证。');
          credentials[key] = value;
        }
        try { await this.credentials.write(name, JSON.stringify(credentials)); }
        catch { throw new UserError('无法加密保存账户凭证，请检查当前 Windows 用户的本机存储。'); }
      } else {
        try { credentials = JSON.parse(await this.credentials.read(name)); }
        catch { throw new UserError('无法读取账户凭证。请使用「连接已有账户」重新填写凭证。'); }
      }
      const response = await this.backend.request(action, { ...data, slot, credentials });
      if (!response.ok) {
        if (enrolling) await this.credentials.remove(name);
        return response;
      }
      this.prepared.clear();
      const retired = response.data.retired_slot;
      delete response.data.retired_slot;
      if (typeof retired === 'string' && /^[0-9a-f]{32}$/.test(retired) && retired !== slot) {
        await this.credentials.remove('foregate-' + retired);
      }
      return response;
    } finally {
      this.switching = false;
    }
  }

  async confirm(action, data) {
    if (this.switching || this.confirming) throw new UserError('请先完成当前确认。');
    this.confirming = true;
    try {
      let title, detail, buttons;
      switch (action) {
        case 'api_execute': {
          const plan = this.prepared.get(data.token);
          this.prepared.delete(data.token);
          if (!plan || plan.context !== data.account_context) throw new UserError('请重新准备后确认。');
          title = '确认 ForeGate 账户操作';
          detail = plan.summary;
          buttons = ['取消', '确认提交'];
          break;
        }
        case 'api_follow_arm':
          if (typeof data.summary !== 'string' || !data.summary.trim()) throw new UserError('请重新检查跟单参数。');
          title = '启用实盘自动跟单？';
          detail = data.summary;
          buttons = ['取消', '启用实盘跟单'];
          break;
        case 'api_mfa_setup':
          title = '为 API 账户绑定验证器？';
          detail = '接下来将显示验证器密钥，请在你的验证器中录入后输入一次验证码。';
          buttons = ['取消', '继续绑定'];
          break;
        case 'api_review_unknown':
          title = '记录人工核对结果';
          detail = '请依据 ForeGate API 账户的订单、成交或资金流水选择结果。\n\n' + String(data.label || '');
          buttons = ['取消', '查到该操作', '确认该操作未执行'];
          break;
      }
      const { response } = await dialog.showMessageBox(this.window, {
        type:'warning', title:'信号跟单', message:title, detail, buttons, defaultId:0, cancelId:0, noLink:true
      });
      if (response === 0) throw new UserError('操作已取消。');
      if (action === 'api_review_unknown') data.outcome = response === 2 ? 'absent' : 'found';
      data.native_authorized = true;
      return await this.backend.request(action, data);
    } finally {
      this.confirming = false;
    }
  }

  async saveSettings(data) {
    const hash = data.api_hash;
    delete data.api_hash;
    if (hash && (typeof hash !== 'string' || !/^[a-f0-9]{32}$/i.test(hash))) {
      throw new UserError('API Hash 应为 32 位十六进制字符。');
    }
    const response = await this.backend.request('save_settings', data);
    if (response.ok && hash) {
      try { await this.credentials.write('telegram-api-hash', hash); }
      catch { throw new UserError('连接参数已保存，请检查本机存储后重新保存 API Hash。'); }
      await this.backend.request('credentials', { api_hash:hash });
      if (response.data.settings) response.data.settings.api_hash_set = true;
    }
    return response;
  }

  async invoke(event, action, input) {
    if (event.sender !== this.window.webContents || !event.senderFrame || event.senderFrame !== this.window.webContents.mainFrame ||
        event.senderFrame.url !== PAGE || !allowed.has(action) || !input || typeof input !== 'object' || Array.isArray(input)) {
      return { ok:false, error:'无效请求。' };
    }
    const data = { ...input };
    delete data.native_authorized;
    try {
      if (action === 'api_account_enroll' || action === 'api_account_select') return await this.changeAccount(action, data);
      if (confirmations.has(action)) return await this.confirm(action, data);
      if (action === 'copy_address') {
        if (this.switching || typeof data.address !== 'string' || !/^\S{1,200}$/.test(data.address)) {
          throw new UserError('充值地址无效或账户正在切换。');
        }
        clipboard.writeText(data.address);
        return { ok:true, data:{ copied:true } };
      }
      if (action === 'save_settings') return await this.saveSettings(data);
      const response = await this.backend.request(action, data);
      if (action === 'api_prepare' && response.ok && response.data?.token && response.data.summary) {
        if (this.prepared.size >= 32) this.prepared.clear();
        this.prepared.set(response.data.token, { summary:response.data.summary, context:data.account_context });
      }
      return response;
    } catch (error) {
      return { ok:false, error:error instanceof UserError ? error.message : SERVICE_ERROR };
    }
  }
}

function openDocumentation(value) {
  const url = URL.parse(value);
  if (!url) return;
  if (url.protocol === 'https:' && externalHosts.has(url.host) && !url.username && !url.password) {
    void shell.openExternal(url.href).catch(() => {});
  }
}

protocol.registerSchemesAsPrivileged([{ scheme:'workspace', privileges:{ standard:true, secure:true } }]);

async function start() {
  await app.whenReady();
  app.setAppUserModelId('local.tgcopytrader.client');
  protocol.handle('workspace', async request => {
    const asset = assets.get(request.url);
    if (!asset || request.method !== 'GET') return new Response(null, { status:404 });
    return new Response(await readFile(join(__dirname, 'ui', asset[0])),
      { headers:{ 'Content-Type':asset[1] + '; charset=utf-8', 'X-Content-Type-Options':'nosniff' } });
  });

  const data = app.getPath('userData');
  const backend = new Backend(process.resourcesPath, join(data, 'workspace'));
  const credentials = new CredentialStore(join(data, 'credentials'));
  const window = new BrowserWindow({
    width:1260, height:840, minWidth:960, minHeight:670, show:false, autoHideMenuBar:true,
    backgroundColor:'#f8f6f5', title:'信号跟单 · ForeGate API 工作台', icon:join(__dirname, 'ui', 'AppIcon.png'),
    webPreferences:{ preload:join(__dirname, 'preload.cjs'), nodeIntegration:false, contextIsolation:true,
      sandbox:true, devTools:false, spellcheck:false }
  });
  window.webContents.on('page-title-updated', event => event.preventDefault());
  window.webContents.session.setPermissionRequestHandler((_contents, _permission, respond) => respond(false));
  window.webContents.session.setPermissionCheckHandler(() => false);
  window.webContents.setWindowOpenHandler(({ url }) => {
    openDocumentation(url);
    return { action:'deny' };
  });
  window.webContents.on('will-frame-navigate', details => {
    if (details.isMainFrame && details.url === PAGE) return;
    details.preventDefault();
    if (details.isMainFrame) openDocumentation(details.url);
  });
  app.on('second-instance', () => {
    if (window.isDestroyed()) return;
    if (window.isMinimized()) window.restore();
    window.focus();
  });
  app.on('window-all-closed', () => app.quit());
  let quitting = false;
  app.on('before-quit', event => {
    if (quitting) return;
    event.preventDefault();
    void backend.stop().finally(() => {
      quitting = true;
      app.quit();
    });
  });

  const workspace = new Workspace(window, backend, credentials);
  const tgHash = await credentials.read('telegram-api-hash').catch(() => '');
  // Queue initialization before the first page request. The page still loads if the service fails.
  void backend.request('credentials', { api_hash:tgHash }).catch(() => {});
  ipcMain.handle('workspace:request', (event, action, input) => workspace.invoke(event, action, input));
  window.once('ready-to-show', () => window.show());
  await window.loadURL(PAGE);
}

if (app.requestSingleInstanceLock()) {
  void start().catch(() => {
    dialog.showErrorBox('信号跟单', '客户端运行组件未能启动。请完整解压安装包后重新打开。');
    app.quit();
  });
} else {
  app.quit();
}
