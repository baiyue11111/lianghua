import type {ReactNode} from 'react';

export type ViewId = 'market' | 'strategies' | 'result' | 'data' | 'run';

const paths: Record<string, string> = {
  market: 'M3 17l5-6 4 3 5-8 4 4',
  strategies: 'M4 19V5m0 14h16M8 15v-4m4 4V8m4 7v-6',
  result: 'M4 6h16M4 12h10M4 18h7',
  data: 'M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zm0 0v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3',
  run: 'M8 5l11 7-11 7V5z',
  refresh: 'M20 11a8 8 0 10-2.3 6M20 5v6h-6',
  search: 'M11 19a8 8 0 100-16 8 8 0 000 16zm6-2l4 4',
  plus: 'M12 5v14M5 12h14',
};

export function Icon({name}: {name: string}) {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d={paths[name] ?? paths.market} /></svg>;
}

export function PageHead({eyebrow, title, subtitle, actions}: {
  eyebrow?: string; title: string; subtitle?: ReactNode; actions?: ReactNode;
}) {
  return <header className="pagehead">
    <div>
      {eyebrow && <div className="eyebrow">{eyebrow}</div>}
      <h1>{title}</h1>
      {subtitle && <div className="sub">{subtitle}</div>}
    </div>
    {actions && <div className="head-actions">{actions}</div>}
  </header>;
}

export function Card({title, hint, extra, children, className, flush, id}: {
  title?: string; hint?: ReactNode; extra?: ReactNode; children: ReactNode;
  className?: string; flush?: boolean; id?: string;
}) {
  return <section className={`card${flush ? ' flush' : ''}${className ? ' ' + className : ''}`} data-od-id={id}>
    {(title || extra) && <div className="cardhead">
      <h2>{title}</h2>
      <div className="inline">{hint && <span className="hint">{hint}</span>}{extra}</div>
    </div>}
    {children}
  </section>;
}

export function Metric({label, value, tone, foot, small}: {
  label: string; value: ReactNode; tone?: 'up' | 'down'; foot?: ReactNode; small?: boolean;
}) {
  return <div className="metric">
    <div className="label">{label}</div>
    <div className={`value num${small ? ' sm' : ''}${tone ? ' ' + tone : ''}`}>{value}</div>
    {foot && <div className="foot">{foot}</div>}
  </div>;
}

export function Tag({children, tone}: {children: ReactNode; tone?: 'blue' | 'green' | 'red' | 'amber'}) {
  return <span className={`tag${tone ? ' ' + tone : ''}`}>{children}</span>;
}

export function Segmented<T extends string>({options, value, onChange, label, disabled}: {
  options: {value: T; label: string}[]; value: T; onChange: (value: T) => void; label?: string;
  disabled?: boolean;
}) {
  return <div className="segmented" role="group" aria-label={label} aria-disabled={disabled || undefined}>
    {options.map(option => <button key={option.value} type="button" aria-pressed={value === option.value}
      disabled={disabled} onClick={() => onChange(option.value)}>{option.label}</button>)}
  </div>;
}

export function Tabs<T extends string>({options, value, onChange, label}: {
  options: {value: T; label: string}[]; value: T; onChange: (value: T) => void; label?: string;
}) {
  return <div className="tabs" role="tablist" aria-label={label}>
    {options.map(option => <button key={option.value} type="button" role="tab" aria-selected={value === option.value}
      onClick={() => onChange(option.value)}>{option.label}</button>)}
  </div>;
}

export function KV({items}: {items: {name: string; value: ReactNode}[]}) {
  return <dl className="kv">{items.map((item, index) => <div key={`${item.name}-${index}`}>
    <dt className="k">{item.name}</dt><dd className="v">{item.value}</dd>
  </div>)}</dl>;
}

export type Column<R> = {
  label: string; render: (row: R) => ReactNode; align?: 'right';
  // A sortable column names the order it produces. A column without a key is a
  // value you read, not one you sort by — offering a sort that silently does
  // nothing is worse than offering none.
  sort?: string;
};

export function DataTable<R>({columns, rows, empty = '暂无数据', sort, order, onSort, onRow}: {
  columns: Column<R>[]; rows: R[]; empty?: string;
  sort?: string; order?: 'asc' | 'desc'; onSort?: (key: string) => void;
  onRow?: (row: R) => void;
}) {
  if (!rows.length) return <div className="empty">{empty}</div>;
  return <div className="tablewrap"><table className="tbl">
    <thead><tr>{columns.map((column, index) => {
      const active = !!column.sort && column.sort === sort;
      const aria = active ? (order === 'asc' ? 'ascending' as const : 'descending' as const) : undefined;
      return <th key={index} className={column.align === 'right' ? 'right' : undefined} aria-sort={aria}>
        {column.sort && onSort
          ? <button type="button" className={`th-sort${active ? ' on' : ''}`}
              onClick={() => onSort(column.sort as string)}>
              {column.label}<span className="mark" aria-hidden="true">{active ? (order === 'asc' ? '↑' : '↓') : '↕'}</span>
            </button>
          : column.label}
      </th>;
    })}</tr></thead>
    <tbody>{rows.map((row, rowIndex) => <tr key={rowIndex} className={onRow ? 'clickable' : undefined}
      onClick={onRow ? () => onRow(row) : undefined}>
      {columns.map((column, index) => <td key={index} className={column.align === 'right' ? 'right' : undefined}>{column.render(row)}</td>)}
    </tr>)}</tbody>
  </table></div>;
}

export function Pager({page, pages, total, unit, onPage}: {
  page: number; pages: number; total: number; unit: string; onPage: (page: number) => void;
}) {
  if (pages <= 1) return <div className="pager"><span className="faint">{total} {unit}</span></div>;
  return <div className="pager">
    <span className="faint">{total} {unit} · 第 {page}/{pages} 页</span>
    <div className="inline">
      <button className="btn sm" type="button" disabled={page <= 1} onClick={() => onPage(page - 1)}>上一页</button>
      <button className="btn sm" type="button" disabled={page >= pages} onClick={() => onPage(page + 1)}>下一页</button>
    </div>
  </div>;
}

export function Empty({children, busy}: {children: ReactNode; busy?: boolean}) {
  return <div className="empty" role="status">{busy ? '读取中…' : children}</div>;
}

export function Notice({children, tone}: {children: ReactNode; tone?: 'error' | 'ok'}) {
  return <div className={`notice${tone ? ' ' + tone : ''}`} role={tone === 'error' ? 'alert' : undefined}>{children}</div>;
}

export function Progress({value, total}: {value: number; total: number}) {
  const width = total > 0 ? Math.min(100, Math.max(0, (value / total) * 100)) : 0;
  return <div className="progress"><span style={{width: `${width}%`}} /></div>;
}

export function Sparkline({points, color = '#0071e3', className = 'spark'}: {
  points: number[]; color?: string; className?: string;
}) {
  if (points.length < 2) return <span className={className} />;
  const min = Math.min(...points), max = Math.max(...points), span = max - min || 1;
  const coords = points.map((value, index) => `${(index / (points.length - 1)) * 100},${30 - ((value - min) / span) * 26}`).join(' ');
  return <span className={className}><svg viewBox="0 0 100 32" preserveAspectRatio="none" style={{width: '100%', height: '100%', display: 'block'}} aria-hidden="true">
    <polyline points={coords} fill="none" stroke={color} strokeWidth="1.6" vectorEffect="non-scaling-stroke" strokeLinejoin="round" strokeLinecap="round" />
  </svg></span>;
}

export function Field({label, children}: {label: string; children: ReactNode}) {
  return <label className="field"><span className="lbl">{label}</span>{children}</label>;
}

