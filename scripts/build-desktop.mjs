/** Build a self-contained local macOS app. No credentials or repository data enter the bundle. */
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createRequire} from 'node:module';
import {spawnSync} from 'node:child_process';
import crypto from 'node:crypto';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const desktop=path.join(root,'desktop'),build=path.join(desktop,'build');
if(process.platform!=='darwin'||process.arch!=='arm64')throw new Error('当前交付目标为 macOS Apple Silicon；其他架构需要独立构建和执行器验收。');
const require=createRequire(path.join(desktop,'package.json'));
const {packager}=require('@electron/packager');
const python=path.join(root,'.local/desktop-build-venv/bin/python');
if(!fs.existsSync(python))throw new Error('先创建 .local/desktop-build-venv 并安装 desktop/requirements-build.txt。');
function run(command,args){const result=spawnSync(command,args,{cwd:root,stdio:'inherit',env:{...process.env,PYTHONPATH:path.join(root,'backend')}});if(result.status!==0)throw new Error(`构建失败：${path.basename(command)} (${result.status})`);}
fs.mkdirSync(build,{recursive:true});
run('/usr/bin/clang',['-fobjc-arc','-fno-modules','-framework','AppKit',path.join(desktop,'assets/icon.m'),'-o',path.join(build,'icon-renderer')]);
run(path.join(build,'icon-renderer'),[build]);
run(process.execPath,[path.join(root,'scripts/build-offline.mjs')]);
if(!process.argv.includes('--reuse-backend'))run(python,['-m','PyInstaller','--noconfirm','--onedir','--name','forge-desktop-backend',
 '--distpath',path.join(build,'python'),'--workpath',path.join(build,'pyinstaller'),'--specpath',build,
 '--paths',path.join(root,'backend'),'--paths',desktop,'--collect-submodules','forge','--collect-data','forge',
 '--collect-submodules','uvicorn','--hidden-import','sqlalchemy.dialects.sqlite','--hidden-import','greenlet',
 '--add-data',path.join(root,'frontend/dist-desktop')+':frontend',
 '--exclude-module','pytest','--exclude-module','playwright','--exclude-module','temporalio',
 path.join(desktop,'backend_entry.py')]);
// Stage an explicit allowlist, never package .local, .venv, imports or tests.
const stage=fs.mkdtempSync(path.join(build,'app-'));
for(const file of ['package.json','main.cjs','preload.cjs','security.cjs','supervisor.cjs','boot.html','boot.cjs','boot.css']){
 if(fs.existsSync(path.join(desktop,file)))fs.copyFileSync(path.join(desktop,file),path.join(stage,file));
 else if(!['boot.cjs','boot.css'].includes(file))throw new Error('缺少桌面资源：'+file);
}
const backend=path.join(fs.mkdtempSync(path.join(build,'runtime-')),'backend');
fs.cpSync(path.join(build,'python/forge-desktop-backend'),backend,{recursive:true,verbatimSymlinks:true});
const electronZip='electron-v44.3.0-darwin-arm64.zip',localZip=path.join(build,electronZip);
let electronZipDir;
if(fs.existsSync(localZip)){
 const expected=require('electron/checksums.json')[electronZip];
 const actual=crypto.createHash('sha256').update(fs.readFileSync(localZip)).digest('hex');
 if(!expected||actual!==expected)throw new Error('Electron 运行时校验失败，不会使用未验证的本地文件。');
 electronZipDir=build;
}
const results=await packager({dir:stage,name:'Agent Forge',appBundleId:'cn.agentforge.desktop',
 appVersion:require('./package.json').version,buildVersion:require('./package.json').version,platform:'darwin',arch:'arm64',electronVersion:'44.3.0',
 out:path.join(desktop,'release'),overwrite:true,asar:true,prune:true,electronZipDir,
 darwinDarkModeSupport:true,icon:path.join(build,'AgentForge.icns'),
 extendInfo:{NSDocumentsFolderUsageDescription:'选择项目目录以导入工作副本；仅在授权范围内执行任务。',
 NSDownloadsFolderUsageDescription:'保存用户选择下载的任务成果。',
 NSDesktopFolderUsageDescription:'访问用户通过文件夹选择器明确选择的项目目录。',
 LSMinimumSystemVersion:'12.0',NSHumanReadableCopyright:'Agent Forge · Local-first AI organization'}});
// Ad-hoc signing is sufficient for this machine's local build, not notarized distribution.
for(const folder of results){
 const app=path.join(folder,'Agent Forge.app');
 // Packager's extraResource copy resolves symlinks into absolute build paths.
 // Preserve Python.framework's relative links ourselves before signing.
 fs.cpSync(backend,path.join(app,'Contents/Resources/backend'),{recursive:true,verbatimSymlinks:true});
 run('/usr/bin/codesign',['--force','--deep','--sign','-',app]);
 run('/usr/bin/codesign',['--verify','--deep','--strict',app]);
 console.log('macOS 本地应用：'+app);
}
