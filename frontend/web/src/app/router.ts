import {useSyncExternalStore} from 'react';

// One address per object. A page that can only be reached by clicking it, and
// that forgets itself on refresh, cannot be shared, bookmarked or returned to.
// The hash carries the whole location: which object, and how the list it came
// from was filtered.
export type SortKey = 'runs' | 'name' | 'total_return' | 'max_drawdown';

export interface ListQuery {
  q: string;
  status: 'all' | 'unverified' | 'failed' | 'idle';
  sort: SortKey;
  order: 'asc' | 'desc';
  page: number;
}

export type Route =
  | {name: 'market'}
  | {name: 'strategies'; list: ListQuery}
  | {name: 'strategy'; key: string}
  | {name: 'newRun'; module: string}
  | {name: 'run'; id: string}
  | {name: 'radar'}
  | {name: 'data'};

export const LIST_PAGE = 20;

export const DEFAULT_LIST: ListQuery = {q: '', status: 'all', sort: 'runs', order: 'desc', page: 1};

const STATUSES: ListQuery['status'][] = ['all', 'unverified', 'failed', 'idle'];
const SORTS: SortKey[] = ['runs', 'name', 'total_return', 'max_drawdown'];

export function parse(hash: string): Route {
  const raw = (hash || '').replace(/^#/, '');
  const [path, search = ''] = raw.split('?');
  const parts = path.split('/').filter(Boolean);
  const query = new URLSearchParams(search);
  if (parts[0] === 'runs') {
    if (parts[1] === 'new') return {name: 'newRun', module: query.get('module') ?? ''};
    return parts[1] ? {name: 'run', id: decodeURIComponent(parts[1])} : {name: 'market'};
  }
  if (parts[0] === 'strategies') {
    if (parts[1]) return {name: 'strategy', key: decodeURIComponent(parts[1])};
    const status = query.get('status') as ListQuery['status'] | null;
    const sort = query.get('sort') as SortKey | null;
    const page = Number(query.get('page'));
    return {name: 'strategies', list: {
      q: query.get('q') ?? '',
      status: status && STATUSES.includes(status) ? status : DEFAULT_LIST.status,
      sort: sort && SORTS.includes(sort) ? sort : DEFAULT_LIST.sort,
      order: query.get('order') === 'asc' ? 'asc' : DEFAULT_LIST.order,
      page: Number.isFinite(page) && page >= 1 ? Math.floor(page) : 1,
    }};
  }
  if (parts[0] === 'radar' || parts[0] === 'data') return {name: parts[0]};
  return {name: 'market'};
}

export function href(route: Route): string {
  switch (route.name) {
    case 'strategies': {
      // Only what differs from the default is written, so a plain visit stays a
      // plain address and a filtered one stays reproducible.
      const query = new URLSearchParams();
      const {list} = route;
      if (list.q) query.set('q', list.q);
      if (list.status !== DEFAULT_LIST.status) query.set('status', list.status);
      if (list.sort !== DEFAULT_LIST.sort || list.order !== DEFAULT_LIST.order) {
        query.set('sort', list.sort); query.set('order', list.order);
      }
      if (list.page > 1) query.set('page', String(list.page));
      const tail = query.toString();
      return `#/strategies${tail ? '?' + tail : ''}`;
    }
    case 'strategy': return `#/strategies/${encodeURIComponent(route.key)}`;
    case 'newRun': return `#/runs/new${route.module ? '?module=' + encodeURIComponent(route.module) : ''}`;
    case 'run': return `#/runs/${encodeURIComponent(route.id)}`;
    case 'radar':
    case 'data': return `#/${route.name}`;
    default: return '#/market';
  }
}

/** The list a page is looking at, with one field replaced. */
export function withList(list: ListQuery, change: Partial<ListQuery>): ListQuery {
  return {...list, ...change};
}

const listeners = new Set<() => void>();
const here = (): string => (typeof window === 'undefined' ? '' : window.location.hash);

let current: Route = parse(here());
let currentHash = here();

function emit() {
  for (const listener of listeners) listener();
}

export function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export const routeNow = (): Route => current;

export function useRoute(): Route {
  return useSyncExternalStore(subscribe, routeNow, routeNow);
}

export function go(route: Route, replace = false) {
  const next = href(route);
  current = route;
  currentHash = next;
  if (typeof window !== 'undefined' && window.location.hash !== next) {
    // Typing in a filter is not a journey: it rewrites the current entry rather
    // than filling the back button with one step per keystroke.
    if (replace) window.history.replaceState(null, '', next);
    else window.location.hash = next;
  }
  emit();
}

if (typeof window !== 'undefined') {
  window.addEventListener('hashchange', () => {
    if (window.location.hash === currentHash) return;
    currentHash = window.location.hash;
    current = parse(currentHash);
    emit();
  });
}
