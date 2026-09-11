'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('forgeDesktop', Object.freeze({
  platform: process.platform,
  selectProjectDirectory: () => ipcRenderer.invoke('forge:select-project-directory'),
  showDataFolder: () => ipcRenderer.invoke('forge:show-data-folder'),
  notifyTask: task => ipcRenderer.invoke('forge:notify-task', task),
}));
