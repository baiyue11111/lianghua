import type {Run,StrategySpec} from '@display/workbench/port';
import {strategyGroups as summarize} from '@display/workbench/research-model';

type ArchivedRun=Run&{effective_config?:Record<string,unknown>};
export function runConfig(run:Run):Record<string,unknown> {
  return (run as ArchivedRun).effective_config??{};
}
export function strategyGroups(runs:Run[],catalog:StrategySpec[]=[]){
  const aliases=new Map<string,Set<string>>();
  for(const spec of catalog)for(const alias of [spec.strategy_id,spec.module]){
    const ids=aliases.get(alias)??new Set<string>();ids.add(spec.strategy_id);aliases.set(alias,ids);
  }
  const normalized=runs.map(run=>{
    const names=[run.strategy_id,runConfig(run).module].filter((v):v is string=>typeof v==='string');
    const ids=new Set(names.flatMap(name=>[...(aliases.get(name)??[])]));
    return {...run,strategy_id:ids.size===1?[...ids][0]:run.strategy_id};
  });
  const originals=new Map(runs.map(run=>[run.id,run]));
  return summarize(normalized,catalog).map(group=>({...group,runs:group.runs.map(run=>originals.get(run.id)!)}));
}
