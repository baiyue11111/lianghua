import {execFileSync} from 'node:child_process';
import {readFileSync,writeFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {compile} from 'json-schema-to-typescript';
const root=fileURLToPath(new URL('../../../',import.meta.url));
const source=JSON.parse(execFileSync(root+'.venv/bin/python',['-c',"import importlib,json; p=importlib.import_module('frontend.ui-protocol'); print(json.dumps({'schema':p.export_schema(),'ports':p.export_ports()},ensure_ascii=False))"],{cwd:root,encoding:'utf8'}));
const schema=source.schema;
const types=structuredClone(schema);
// Conditional runtime rules do not change the declared action fields.
delete types.$defs.UiAction.allOf;
const outputs={
  'protocol.schema.json':JSON.stringify(schema,null,2)+'\n',
  'protocol.d.ts':await compile(types,'Wire',{bannerComment:'/* Generated from frontend/ui-protocol.py. Do not edit. */',additionalProperties:false})+'\n'+source.ports,
};
for(const [name,data] of Object.entries(outputs)){
  const path=new URL('../src/'+name,import.meta.url);
  if(process.argv.includes('--check')){if(readFileSync(path,'utf8')!==data)throw Error(name+' is stale');}
  else writeFileSync(path,data);
}
