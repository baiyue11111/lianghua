import type {FormEvent} from 'react';
import {useEffect} from 'react';
import type {StrategyCatalog, StrategySpec} from '@display/research/port';
import {comparisonIssue, parameterValues} from '@display/research/strategy-model';
import type {Completed} from '@display/research/strategy-model';
import {useOperation, useSlot, useValue} from '../interaction/model';
import {useBacktestJob} from './job';
import {day, errorMessage, number, percent, text} from '../shared/format';
import {go, DEFAULT_LIST} from '../app/router';
import {ARCHIVE_QUERY} from './queries';
import {Card, Empty, KV, Metric, Notice, PageHead, Tag} from './ui';
import {summary} from './summary';

const TARGET = 'apple:run:backtest';
const dateBack = (days: number) => new Date(Date.now() - days * 86400000).toISOString().slice(0, 10);

export default function Run({module: picked}: {module: string}) {
  const [stored, setModule] = useValue('apple:run', 'module', '');
  // Arriving from a strategy's own page carries it in the address. The address
  // is the source of truth, so choosing another strategy rewrites it instead of
  // leaving the field and the URL saying different things.
  const module = picked || stored;
  const [capital, setCapital] = useValue('apple:run', 'capital', '1000000');
  const [start, setStart] = useValue('apple:run', 'start', dateBack(365));
  const [end, setEnd] = useValue('apple:run', 'end', dateBack(0));
  const [dataset, setDataset] = useValue('apple:run', 'dataset', '');
  const [overrides, setOverrides] = useValue<Record<string, string | boolean>>('apple:run', 'values', {});
  const [configPath, setConfigPath] = useValue('apple:run', 'configPath', '');
  // Two forms, two paths: sharing one field made editing the prediction inputs
  // silently rewrite the backtest file path.
  const [predictConfig, setPredictConfig] = useValue('apple:run', 'predictConfig', '');
  const [accountPath, setAccountPath] = useValue('apple:run', 'accountPath', '');

  const catalog = useOperation<StrategyCatalog>('apple:catalog', 'backtest.strategies', {}, true, true, {fresh: 60_000});
  const {invoke: refreshArchives} = useOperation('apple:archives', 'research.runs', ARCHIVE_QUERY, true, true, {fresh: 30_000});
  const cliBacktest = useOperation<unknown>('apple:run:cli', 'cli.backtest', {}, true, false);
  const cliPredict = useOperation<unknown>('apple:run:predict', 'cli.predict', {}, true, false);
  const specs = catalog.value?.items ?? [];
  const spec: StrategySpec | undefined = specs.find(item => item.module === module) ?? specs[0];
  const datasets = catalog.value?.availability?.datasets ?? [];
  // Merging the defaults with what was typed is a spread of two small objects;
  // memoizing it bought nothing and made the compiler give up on the component.
  const values = spec ? {...parameterValues(spec), ...overrides} : {};
  const form = {spec, values, capital, start, end, dataset: dataset || datasets[0]?.id || ''};
  const ready = !!spec && !!form.dataset && !!capital.trim() && !!start && !!end && start <= end;

  const execute = useBacktestJob(TARGET, form);
  const comparison = useSlot(`${TARGET}:comparison`);
  const baseline = comparison.values.baseline as unknown as Completed | undefined;
  const latest = comparison.values.latest as unknown as Completed | undefined;
  const issue = baseline && latest ? comparisonIssue(baseline, latest) : '';
  const canRun = ready && execute.status !== 'pending';
  const job = execute.job;
  const progress = job?.trials_total
    ? `回测运行中：第 ${job.trials_done}/${job.trials_total} 次试验`
    : job ? (job.phase === 'search' ? '回测运行中：参数搜索' : '回测运行中…') : '已提交，等待后端开始…';

  function submitRun(event: FormEvent) {
    event.preventDefault();
    if (!canRun) return;
    // The run stays on this page so the comparison with the previous attempt is
    // still readable; its result has its own address, one click away.
    void execute.invoke(form);
  }
  // The archive list is read on arrival; a finished run is a new attempt, so the
  // list has to be asked again once the job reports it is done.
  useEffect(() => {
    if (execute.job?.status === 'succeeded') void refreshArchives();
  }, [execute.job?.status, refreshArchives]);
  function submitConfig(event: FormEvent) {
    event.preventDefault();
    const value = configPath.trim();
    if (value) void cliBacktest.invoke({config: value});
  }
  function submitPredict(event: FormEvent) {
    event.preventDefault();
    const path = predictConfig.trim(), account = accountPath.trim();
    if (path && account) void cliPredict.invoke({config: path, account});
  }

  return <>
    <PageHead eyebrow="Run" title="新建回测"
      actions={<>
        <button className="btn" type="button"
          onClick={() => go(module ? {name: 'strategy', key: strategyId(module, specs)} : {name: 'strategies', list: DEFAULT_LIST})}>
          {module ? '返回策略详情' : '返回策略列表'}
        </button>
        <Tag tone={catalog.value?.availability?.ready ? 'green' : 'amber'}>
          {catalog.value?.availability?.ready ? '目录就绪' : '目录状态待确认'}</Tag>
      </>} />

    <div className="grid" style={{gridTemplateColumns: 'minmax(0, 1.15fr) minmax(0, 1fr)', alignItems: 'start'}}>
      <Card title="回测配置">
        <form onSubmit={submitRun}>
          <div className="form-grid">
            <label className="field"><span className="lbl">策略</span>
              <select className="input" aria-label="策略" value={spec?.module ?? ''}
                onChange={event => {
                  setModule(event.target.value);
                  setOverrides({});
                  go({name: 'newRun', module: event.target.value}, true);
                }}>
                {specs.map(item => <option key={item.module} value={item.module}>{text(item.name) ?? '策略名称未提供'}</option>)}
              </select>
            </label>
            <label className="field"><span className="lbl">数据集</span>
              <select className="input" aria-label="数据集" value={form.dataset}
                onChange={event => setDataset(event.target.value)}>
                <option value="">选择数据集</option>
                {datasets.map(item => <option key={item.id} value={item.id}>{text(item.name) ?? item.id}</option>)}
              </select>
            </label>
            <label className="field"><span className="lbl">初始资金</span>
              <input className="input" aria-label="初始资金" inputMode="decimal" value={capital}
                onChange={event => setCapital(event.target.value)} />
            </label>
            <label className="field"><span className="lbl">策略说明</span>
              {/* Read-only text must not be dressed as a field: a boxed, greyed
                  input reads as something you can type into. */}
              <div className="muted" style={{fontSize: 12, lineHeight: 1.7, paddingTop: 6}}>
                {text(spec?.description) ?? '说明未提供'}
              </div>
            </label>
            <label className="field"><span className="lbl">起始日期</span>
              <input className="input" type="date" aria-label="起始日期" value={start}
                onChange={event => setStart(event.target.value)} />
            </label>
            <label className="field"><span className="lbl">截止日期</span>
              <input className="input" type="date" aria-label="截止日期" value={end}
                onChange={event => setEnd(event.target.value)} />
            </label>
          </div>

          {!!spec?.parameters.length && <>
            <div className="divider" />
            <div className="section-title">策略参数</div>
            <div className="form-grid">
              {spec.parameters.map(field => <label className="field" key={field.key}>
                <span className="lbl">{text(field.label) ?? field.key}{field.unit ? `（${field.unit}）` : ''}</span>
                {field.type === 'boolean'
                  ? <span className="check"><input type="checkbox" checked={values[field.key] === true}
                      onChange={event => setOverrides(current => ({...current, [field.key]: event.target.checked}))} />开启</span>
                  : field.options?.length
                    ? <select className="input" value={String(values[field.key] ?? '')}
                        onChange={event => setOverrides(current => ({...current, [field.key]: event.target.value}))}>
                        {field.options.map(option => <option key={String(option)} value={String(option)}>{String(option)}</option>)}
                      </select>
                    : <input className="input" value={String(values[field.key] ?? '')}
                        onChange={event => setOverrides(current => ({...current, [field.key]: event.target.value}))} />}
              </label>)}
            </div>
          </>}

          <div className="inline" style={{marginTop: 18}}>
            <button className="btn primary" type="submit" disabled={!canRun}>运行回测</button>
          </div>
        </form>
        {!specs.length && <Empty busy={catalog.status === 'pending'}>策略目录为空，请检查服务是否已登记策略</Empty>}
        {catalog.error && <Notice tone="error">{errorMessage(catalog.error)}</Notice>}
      </Card>

      <div className="grid" style={{gap: 18}}>
        <Card title="运行与对比">
          {execute.error && <Notice tone="error">{execute.error}</Notice>}
          {!execute.value ? <Empty busy={execute.status === 'pending'}>
            {execute.status === 'pending' ? progress : '运行后在这里查看结果与对比结论'}
          </Empty> : <>
            <div className="grid g2" style={{gap: 12, margin: '16px 0'}}>
              <Metric label="累计收益" value={percent(execute.value.metrics.total_return)} small />
              <Metric label="最大回撤" value={percent(execute.value.metrics.max_drawdown)} small />
              <Metric label="夏普比率" value={number(execute.value.metrics.sharpe, 3)} small />
              <Metric label="观测数" value={number(execute.value.metrics.observations, 0)} small />
            </div>
            <KV items={[
              {name: '期末', value: day(execute.value.equity.at(-1)?.at)},
              {name: '权益观测', value: execute.value.equity.length},
              {name: '成交', value: number(execute.value.trades.length, 0)},
            ]} />
            <div className="divider" />
            <div className="section-title">与基线对比</div>
            {issue ? <Notice tone="error">{issue}</Notice>
              : <div className="muted" style={{fontSize: 12}}>{baseline ? '两次运行的数据与执行身份一致，结果可比。' : '首次运行，暂无对比基线。'}</div>}
            <div className="inline" style={{marginTop: 14}}>
              <button className="btn sm primary" type="button"
                onClick={() => go({name: 'run', id: execute.value?.id ?? ''})}>打开这次回测</button>
              <button className="btn sm" type="button" onClick={() => void refreshArchives()}>刷新存档</button>
            </div>
          </>}
        </Card>

        <Card title="请求摘要" hint="提交前可核对">
          <KV items={summary({
            策略: text(spec?.name) ?? '未提供', 数据集: form.dataset || '未提供',
            初始资金: capital, 起始日期: start, 截止日期: end,
            参数项: Object.keys(values).length,
          }, 6)} />
        </Card>

        {/* Server-side config runs stay on the operation surface, collapsed:
            they take a filesystem path, which is a backend concept and must not
            sit in the primary reading flow. */}
        <details className="adv">
          <summary>高级 · 用服务器配置文件运行</summary>
          <div className="adv-body">
            <form onSubmit={submitConfig}>
              <label className="field"><span className="lbl">配置文件路径</span>
                <input className="input" aria-label="配置文件路径" placeholder="/server/config.json"
                  value={configPath} onChange={event => setConfigPath(event.target.value)} /></label>
              <div className="inline" style={{marginTop: 12}}>
                <button className="btn sm" type="submit"
                  disabled={!configPath.trim() || cliBacktest.status === 'pending'}>按文件运行回测</button>
                <span className="faint" style={{fontSize: 11}}>参数沿用文件内容</span>
              </div>
            </form>
            {cliBacktest.value != null && <div style={{marginTop: 12}}><KV items={summary(cliBacktest.value, 5)} /></div>}
            {cliBacktest.error && <Notice tone="error">{errorMessage(cliBacktest.error)}</Notice>}

            <div className="divider" />
            <form onSubmit={submitPredict}>
              <div className="grid g2" style={{gap: 10}}>
                <label className="field"><span className="lbl">策略配置</span>
                  <input className="input" aria-label="预测配置路径" placeholder="config.json"
                    value={predictConfig} onChange={event => setPredictConfig(event.target.value)} /></label>
                <label className="field"><span className="lbl">账户文件</span>
                  <input className="input" aria-label="账户文件路径" placeholder="account.json"
                    value={accountPath} onChange={event => setAccountPath(event.target.value)} /></label>
              </div>
              <div className="inline" style={{marginTop: 12}}>
                <button className="btn sm" type="submit"
                  disabled={!predictConfig.trim() || !accountPath.trim() || cliPredict.status === 'pending'}>生成下一交易日计划</button>
              </div>
            </form>
            {cliPredict.value != null && <div style={{marginTop: 12}}><KV items={summary(cliPredict.value, 5)} /></div>}
            {cliPredict.error && <Notice tone="error">{errorMessage(cliPredict.error)}</Notice>}
          </div>
        </details>
      </div>
    </div>
  </>;
}

// A route names a strategy by the identity its archives are stored under, not
// by the module the catalogue happens to file it under.
function strategyId(module: string, specs: StrategySpec[]): string {
  return specs.find(item => item.module === module)?.strategy_id ?? module;
}
