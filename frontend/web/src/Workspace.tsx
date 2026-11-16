import type {StrategyCatalog} from '@display/research/port';
import {useValue, useOperation} from './interaction/model';
import Data from './apple/Data';
import Market from './apple/Market';
import Result from './apple/Result';
import RunView from './apple/Run';
import Strategies from './apple/Strategies';
import {Icon} from './apple/ui';
import type {ViewId} from './apple/ui';
import './apple/theme.css';

const NAV: {id: ViewId; label: string; icon: string}[] = [
  {id: 'market', label: '行情', icon: 'market'},
  {id: 'strategies', label: '策略研究', icon: 'strategies'},
  {id: 'result', label: '回测结果与配置', icon: 'result'},
  {id: 'data', label: '数据中心', icon: 'data'},
  {id: 'run', label: '新建回测', icon: 'run'},
];
const VIEWS: ViewId[] = NAV.map(item => item.id);
// One navigation model, one place. The top bar carries identity and global
// status; the sidebar owns page switching. Repeating the same five links in
// both bars made the user read two identical menus before deciding.
const GROUPS: {label: string; items: ViewId[]}[] = [
  {label: '工作区', items: ['market', 'strategies', 'result']},
  {label: '数据与执行', items: ['data', 'run']},
];

function normalize(raw: string): ViewId {
  if (raw === 'research') return 'strategies';
  return VIEWS.includes(raw as ViewId) ? raw as ViewId : 'market';
}

export default function Workspace() {
  const [rawView, setRawView] = useValue('workspace', 'page', 'market');
  const view = normalize(rawView);
  const navigate = (next: ViewId) => setRawView(next);
  const service = useOperation<{root?: string}>('apple:shell:service', 'service.info');
  const catalog = useOperation<StrategyCatalog>('apple:catalog', 'backtest.strategies', {}, true, true, {fresh: 60_000});
  const online = !!service.value?.root;

  return <div className="apple">
    <header className="topbar">
      <div className="brand"><span className="brand-mark" />量研 <span className="faint" style={{fontWeight: 400, fontSize: 12}}>Quant Terminal</span></div>
      <div className="topmeta">
        <span className={`dot${online ? '' : ' off'}`} />
        <span>{online ? '服务已连接' : '服务状态待确认'}</span>
        <span className="sep">·</span>
        <span>{catalog.value?.items?.length ?? '—'} 个策略</span>
      </div>
    </header>

    <div className="layout">
      <aside className="sidebar" data-od-id="navigation">
        {GROUPS.map(group => <div className="side-group" key={group.label}>
          <div className="side-label">{group.label}</div>
          {group.items.map(id => {
            const item = NAV.find(entry => entry.id === id)!;
            return <button key={item.id} type="button" className="side-item"
              data-od-id={`nav-${item.id}`} aria-current={view === item.id ? 'page' : undefined}
              onClick={() => navigate(item.id)}>
              <span className="ico"><Icon name={item.icon} /></span>{item.label}
            </button>;
          })}
        </div>)}
      </aside>

      <main className="content" data-od-id="workspace">
        {view === 'market' && <Market />}
        {view === 'strategies' && <Strategies navigate={navigate} />}
        {view === 'result' && <Result navigate={navigate} />}
        {view === 'data' && <Data />}
        {view === 'run' && <RunView navigate={navigate} />}
      </main>
    </div>
  </div>;
}
