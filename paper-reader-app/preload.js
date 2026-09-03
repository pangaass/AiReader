const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('paperReader', {
  getSettings: () => ipcRenderer.invoke('settings:get'),
  saveSettings: (settings) => ipcRenderer.invoke('settings:save', settings),
  buildVisualizer: (filePath, paperId) => ipcRenderer.invoke('visualizer:build', filePath, paperId),
  onBuildProgress: (callback) => {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on('visualizer:progress', listener);
    return () => ipcRenderer.removeListener('visualizer:progress', listener);
  },
  getSamples: () => ipcRenderer.invoke('library:samples'),
  chooseFolder: () => ipcRenderer.invoke('library:choose-folder'),
  choosePaper: () => ipcRenderer.invoke('paper:choose'),
  loadPaper: (filePath, llm) => ipcRenderer.invoke('paper:load', filePath, llm),
  notifyReady: () => ipcRenderer.send('ui:ready'),
});
