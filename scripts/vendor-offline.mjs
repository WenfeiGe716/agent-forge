/** Assemble already-installed MIT runtime modules. Never downloads or invents package metadata.
 * Maintainer-only helper; distributed vendor files carry checksums and upstream version/provenance.
 */
import fs from 'node:fs'; import vm from 'node:vm'; import path from 'node:path'; import crypto from 'node:crypto';
const root=path.resolve(import.meta.dirname,'..');
const source='/opt/pyvenv/lib/python3.13/site-packages/notebook/static';
const parts=['7378.df12091e8f42a5da0429.js','1542.8f0b79431f7af2f43f1e.js','7811.fa11577c84ea92d4102c.js'];
const modules={};const context={self:{webpackChunk_JUPYTERLAB_CORE_OUTPUT:{push:([_,m])=>Object.assign(modules,m)}}};
for(const file of parts)vm.runInNewContext(fs.readFileSync(path.join(source,file),'utf8'),context);
let out='/* React / React DOM / Scheduler 18.2.0. MIT. See THIRD_PARTY_NOTICES.md. */\nconst factories={\n';
// Only React, ReactDOM, scheduler and JSX runtime. No notebook application code.
for(const id of [41535,27378,43577,73323,91102,71426,24246]){
 if(!modules[id])throw Error('Missing runtime module '+id);
 out+=JSON.stringify(id)+':'+modules[id].toString()+',\n';
}
out+='};\nconst cache={};function require(id){if(id===78156)return require(27378);if(cache[id])return cache[id].exports;const module={exports:{}};cache[id]=module;if(!factories[id])throw Error("Missing vendored module "+id);factories[id](module,module.exports,require);return module.exports;}\nexport const React=require(27378);export const ReactDOM=require(43577);export const JSX=require(24246);\n';
const dir=path.join(root,'frontend/vendor');fs.mkdirSync(dir,{recursive:true});fs.writeFileSync(path.join(dir,'react-runtime.js'),out);
fs.writeFileSync(path.join(dir,'react.js'),'import {React} from "./react-runtime.js"; export default React; export const {useState,useEffect,useMemo,useCallback,useRef,useReducer,useLayoutEffect,createElement,Fragment,StrictMode,Component,createContext,useContext,useSyncExternalStore}=React;\n');
fs.writeFileSync(path.join(dir,'react-dom-client.js'),'import {ReactDOM} from "./react-runtime.js"; export const {createRoot,hydrateRoot}=ReactDOM;\n');
fs.writeFileSync(path.join(dir,'jsx-runtime.js'),'import {JSX} from "./react-runtime.js"; export const {jsx,jsxs,Fragment}=JSX;\n');
fs.copyFileSync('/usr/local/slides_js/node_modules/typescript/lib/typescript.js',path.join(dir,'typescript.cjs'));
const files=fs.readdirSync(dir).filter(x=>x.endsWith('.js')||x.endsWith('.cjs'));
const manifest={runtime:'React 18.2.0, ReactDOM 18.2.0, scheduler as bundled by Notebook',compiler:'TypeScript 5.8.3 (installed compiler; emit only)',provenance:parts.map(x=>({package:'notebook/static/'+x,sha256:crypto.createHash('sha256').update(fs.readFileSync(path.join(source,x))).digest('hex')})),files:Object.fromEntries(files.map(x=>[x,crypto.createHash('sha256').update(fs.readFileSync(path.join(dir,x))).digest('hex')]))};
fs.writeFileSync(path.join(dir,'MANIFEST.json'),JSON.stringify(manifest,null,2));console.log(JSON.stringify(manifest,null,2));
