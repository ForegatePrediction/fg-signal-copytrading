'use strict';

const { spawn } = require('node:child_process');
const { join } = require('node:path');
const { createInterface } = require('node:readline');

const SERVICE_ERROR = '本地服务已停止。请重新打开客户端，并先核对上次提交的订单。';

class Backend {
  constructor(resources, dataDir) {
    this.pending = new Map();
    this.sequence = 0;
    this.closed = false;
    const child = spawn(join(resources, 'python', 'python.exe'),
      ['-I', '-B', '-X', 'utf8', '-u', '-m', 'copytrader.desktop_service',
        '--data-dir', dataDir, '--api-client'],
      { cwd:join(resources, 'backend'), windowsHide:true, stdio:['pipe', 'pipe', 'ignore'] });
    this.child = child;
    const lines = createInterface({ input:child.stdout });
    lines.on('line', line => {
      let response;
      try { response = JSON.parse(line); }
      catch { return; }
      const pending = this.pending.get(response?.id);
      if (!pending) return;
      this.pending.delete(response.id);
      pending.resolve(response);
    });
    this.exit = new Promise(resolve => {
      const finish = () => {
        this.closed = true;
        for (const pending of this.pending.values()) pending.reject(new Error(SERVICE_ERROR));
        this.pending.clear();
        resolve();
      };
      child.once('error', finish);
      child.once('close', finish);
    });
    child.stdin.on('error', () => { /* Individual writes report their errors below. */ });
  }

  request(action, data = {}) {
    if (this.closed || !this.child.stdin.writable) return Promise.reject(new Error(SERVICE_ERROR));
    const id = ++this.sequence;
    const line = JSON.stringify({ id, action, data }) + '\n';
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.child.stdin.write(line, 'utf8', error => {
        if (!error) return;
        this.pending.delete(id);
        reject(new Error(SERVICE_ERROR));
      });
    });
  }

  async stop() {
    if (this.closed) return;
    const forced = setTimeout(() => this.child.kill(), 5000);
    try {
      void this.request('shutdown').catch(() => {});
      this.child.stdin.end();
      await this.exit;
    } finally {
      clearTimeout(forced);
    }
  }
}

module.exports = { Backend, SERVICE_ERROR };
