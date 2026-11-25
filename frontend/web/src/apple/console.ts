import {createContext, useContext, useEffect, useState} from 'react';

/** The compact state a control console shows. Mirrors collection.console. */
export interface ConsoleState {
  market: string;
  at: number;
  enabled: boolean;
  running: boolean;
  scheduler: boolean;
  phase: string | null;
  source_id: string | null;
  records: number;
  next_run_at: number | null;
  last_run_at: string | null;
  last_outcome: string | null;
  error: string | null;
}

export type StreamStatus = 'connecting' | 'live' | 'retrying';

export interface ConsoleSnapshot {
  value?: ConsoleState;
  status: StreamStatus;
}

/**
 * A static snapshot for render-time checks. A socket cannot exist during a
 * server render, so without this seam the console would always look "connecting".
 * Production never provides it: the stream is the only live source.
 */
export const ConsoleContext = createContext<ConsoleSnapshot | undefined>(undefined);

/**
 * One read stream for the whole console.
 *
 * The browser does not poll: EventSource keeps a single GET open, reconnects on
 * its own, and the server pushes a compact snapshot every second. Commands are
 * still discrete POST operations — a stream is for observation, never for acting.
 */
export function useConsole(enabled = true): ConsoleSnapshot {
  const injected = useContext(ConsoleContext);
  const [value, setValue] = useState<ConsoleState | undefined>();
  const [status, setStatus] = useState<StreamStatus>('connecting');
  const [, setTick] = useState(0);

  useEffect(() => {
    if (!enabled || injected) return;
    const source = new EventSource('/api/console/stream');
    source.addEventListener('console', event => {
      try {
        setValue(JSON.parse((event as MessageEvent<string>).data) as ConsoleState);
        setStatus('live');
      } catch {
        setStatus('retrying');
      }
    });
    source.addEventListener('error', () => setStatus('retrying'));
    source.onopen = () => setStatus('live');
    source.onerror = () => setStatus('retrying');
    return () => source.close();
  }, [enabled, injected]);

  // "Updated 3s ago" needs the clock to move even when the state did not.
  // Re-rendering on a timer is what makes the "updated N seconds ago" label
  // move while the underlying state is unchanged.
  useEffect(() => {
    const timer = setInterval(() => setTick(value => value + 1), 5000);
    return () => clearInterval(timer);
  }, []);

  return injected ?? {value, status};
}
