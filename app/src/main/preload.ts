import { contextBridge, ipcRenderer } from 'electron'

contextBridge.exposeInMainWorld('desktop', {
  openFile: (filters?: unknown) => ipcRenderer.invoke('open-file', filters),
  saveFile: (name: string) => ipcRenderer.invoke('save-file', name),
  chooseFolder: () => ipcRenderer.invoke('choose-folder'),
  reveal: (path: string) => ipcRenderer.invoke('reveal', path),
  openPath: (path: string) => ipcRenderer.invoke('open-path', path),
  info: () => ipcRenderer.invoke('info'),
  onOpenShare: (fn: (path: string) => void) => ipcRenderer.on('open-share', (_e, p) => fn(p)),
})
