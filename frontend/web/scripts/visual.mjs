// Generate a local controlled test surface. It has no production backend transport.
import {build} from '../../display/node_modules/esbuild/lib/main.js';
import {readFile,writeFile,mkdtemp} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve,dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
const web=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const directory=await mkdtemp(join(tmpdir(),'readable-visual-'));
const result=await build({entryPoints:[join(web,'scripts/visual.fixture.jsx')],bundle:true,write:false,platform:'browser',format:'iife',jsx:'automatic',mainFields:['module','main'],alias:{'@display':resolve(web,'../display/src')},loader:{'.css':'empty'}});
const css=await readFile(join(web,'src/shared/app.css'),'utf8');
const file=join(directory,'readable-check.html');
await writeFile(file,'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>body{margin:0}'+css+'</style></head><body><div id="root"></div><script>'+result.outputFiles[0].text.replace(/<\/script/gi,'<\\/script')+'</script></body></html>');
console.log(file);
console.log('Controlled scenes: #market #research #attempts #result #config #trades #batch');
