import type {StrategyCatalog} from '@display/research/port';
import {useOperation} from './interaction/model';
import {DEFAULT_LIST, go, useRoute} from './app/router';
import type {Route} from './app/router';
import Data from './apple/Data';
import Market from './apple/Market';
import Radar from './apple/Radar';
import Result from './apple/Result';
import RunView from './apple/Run';
import Strategies from './apple/Strategies';
import StrategyDetail from './apple/StrategyDetail';
import {Icon} from './apple/ui';
import './apple/theme.css';

const NAV: {id: string; label: string; icon: string; route: Route}[] = [
  {id: 'market', label: '行情', icon: 'market', route: {name: 'market'}},
  {id: 'strategies', label: '策略研究', icon: 'strategies', route: {name: 'strategies', list: DEFAULT_LIST}},
  {id: 'radar', label: '雷达', icon: 'radar', route: {name: 'radar'}},
  {id: 'data', label: '数据中心', icon: 'data', route: {name: 'data'}},
];

// The sidebar lists destinations, not actions and not consequences: "新建回测"
// is something you do to a strategy and a run is something you open from one,
// so neither belongs in the top-level list.
//
// Every strategy screen still belongs to one section, so the sidebar can say
// where you are even when the second level is a detail.
const SECTION: Record<Route['name'], string> = {
  market: 'market', radar: 'radar', data: 'data',
  strategies: 'strategies', strategy: 'strategies', newRun: 'strategies', run: 'strategies',
};

export default function Workspace() {
  const route = useRoute();
  const service = useOperation<{root?: string}>('apple:shell:service', 'service.info');
  const catalog = useOperation<StrategyCatalog>('apple:catalog', 'backtest.strategies', {}, true, true, {fresh: 60_000});
  const online = !!service.value?.root;
  const section = SECTION[route.name];

  return <div className="apple">
    <header className="topbar">
      {/* The words, not the money, are the way home. */}
      <a className="brand" href="#/market"><span className="brand-mark" />量研 <span className="faint" style={{fontWeight: 400, fontSize: 12}}>Quant Terminal</span></a>
      <div className="topmeta">
        <span className={`dot${online ? '' : ' off'}`} />
        <span>{online ? '服务已连接' : '服务状态待确认'}</span>
        <span className="sep">·</span>
        <span>{catalog.value?.items?.length ?? '—'} 个策略</span>
      </div>
    </header>

    <div className="layout">
      <aside className="sidebar" data-od-id="navigation">
        <div className="side-group">
          <div className="side-label">工作区</div>
          {NAV.map(item => <button key={item.id} type="button" className="side-item"
            data-od-id={`nav-${item.id}`} aria-current={section === item.id ? 'page' : undefined}
            onClick={() => go(item.route)}>
            <span className="ico"><Icon name={item.icon} /></span>{item.label}
          </button>)}
        </div>
      </aside>

      <main className="content" data-od-id="workspace">
        {route.name === 'market' && <Market />}
        {route.name === 'strategies' && <Strategies list={route.list} />}
        {route.name === 'strategy' && <StrategyDetail strategyKey={route.key} />}
        {route.name === 'newRun' && <RunView module={route.module} />}
        {route.name === 'run' && <Result runId={route.id} />}
        {route.name === 'radar' && <Radar />}
        {route.name === 'data' && <Data />}
      </main>
    </div>
  </div>;
}
