/** Offline semantic TypeScript check using pinned upstream declarations. */
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const require=createRequire(import.meta.url),ts=require(path.join(root,'frontend/vendor/typescript.cjs'));
const base=path.join(root,'frontend');
const parsed=ts.parseJsonConfigFileContent(JSON.parse(fs.readFileSync(path.join(base,'tsconfig.json'),'utf8')),ts.sys,base);
const options={...parsed.options,types:['react','react-dom'],typeRoots:[path.join(base,'vendor/types')],baseUrl:base,paths:{react:['vendor/types/react'], 'react/*':['vendor/types/react/*'], 'react-dom/*':['vendor/types/react-dom/*'],csstype:['vendor/types/csstype']}};
const shimPath=path.join(base,'vendor','typecheck-shim.d.ts');
fs.writeFileSync(shimPath, `import 'csstype';\ndeclare module 'csstype' { interface Properties<TLength = string | 0, TTime = string | 0> { [key: \`--\${string}\`]: string | number | undefined; } }\n`);
const host=ts.createCompilerHost(options);
host.getDefaultLibLocation=()=>path.join(base,'vendor/typescript-lib');
host.getDefaultLibFileName=()=>path.join(base,'vendor/typescript-lib/lib.es2022.full.d.ts');
const program=ts.createProgram([...parsed.fileNames,shimPath],options,host);
const diagnostics=[...parsed.errors,...ts.getPreEmitDiagnostics(program)];
if(diagnostics.length)console.error(ts.formatDiagnosticsWithColorAndContext(diagnostics,{getCanonicalFileName:f=>f,getCurrentDirectory:()=>root,getNewLine:()=> '\n'}));
console.log(JSON.stringify({check:'TypeScript semantic',typescript:ts.version,files:parsed.fileNames.length,errors:diagnostics.length}));
try{fs.unlinkSync(shimPath);}catch{}
if(diagnostics.length)process.exit(1);
