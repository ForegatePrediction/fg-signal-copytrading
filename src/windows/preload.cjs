'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('desktop', {
  clientVersion: 'Windows 客户端 · 0.8.2',
  credentialStorage: '由 Windows 当前用户保护的本机存储中',
  invoke: (action, data) => ipcRenderer.invoke('workspace:request', action, data)
});
