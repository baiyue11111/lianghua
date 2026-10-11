import {build} from 'esbuild';
import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';
const check=process.argv.includes('--check');
const options={bundle:true,platform:'node',format:'esm',target:'node22',logLevel:'warning',write:false,
 banner:{js:'// Generated from frontend/display/src and frontend/cli.mjs. Do not edit.'}};
const outputs=[['../src/core.ts','../dist/core.mjs'],['../src/features.ts','../dist/features.mjs'],['../../cli.mjs','../runtime/cli.mjs']];
for(const [entry,target] of outputs){
 if(check&&!target.includes('runtime'))continue;
 const outfile=new URL(target,import.meta.url);
 const result=await build({...options,entryPoints:[new URL(entry,import.meta.url).pathname],outfile:outfile.pathname});
 const content=result.outputFiles[0].text;
 if(check){if(readFileSync(outfile,'utf8')!==content)throw Error('CLI runtime is stale');}
 else {mkdirSync(new URL('.',outfile),{recursive:true});writeFileSync(outfile,content);}
}
