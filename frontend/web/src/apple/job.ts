import {useCallback, useEffect, useState} from 'react';
import type {BacktestResult} from '@display/research/port';
import {useOperation} from '../interaction/model';

export type BacktestJob = {
  job_id: string | null; running: boolean; status: string; phase: string | null;
  trials_done: number; trials_total: number; started_at: string | null;
  finished_at: string | null; error: string | null; code: string | null;
  result: BacktestResult | null;
};

type SubmitReply = {job_id?: string | null; status?: string};

const POLL_MS = 800;

/**
 * A backtest is minutes long; a request may hold the connection for twenty
 * seconds. So the page submits a job and polls it, which keeps the work inside
 * the one backend instead of in a second process that would open the data file
 * behind its back. The returned shape mirrors `useOperation` so a view reads a
 * run the same way whether it came from a job or from a synchronous call.
 */
export function useBacktestJob(target: string, form: unknown) {
  const submit = useOperation<SubmitReply>(target, 'backtest.submit', form, true, false);
  const poll = useOperation<BacktestJob>(target, 'backtest.job', {}, false, false);
  const [watching, setWatching] = useState(false);
  const job = poll.value;

  const invoke = useCallback(async (input?: unknown) => {
    setWatching(false);
    const state = await submit.invoke(input);
    const slot = (state as {targets?: Record<string, {problems?: unknown[]}>}).targets?.[target];
    // A refused submit (busy, no approval) starts nothing, and polling would
    // then report whichever job is already running as if it were this one.
    if (!slot?.problems?.length) {
      setWatching(true);
      void poll.invoke();
    }
    return state;
  }, [submit.invoke, poll.invoke, target]);

  useEffect(() => {
    if (!watching) return;
    if (job && !job.running) return;
    const timer = setTimeout(() => void poll.invoke(), job ? POLL_MS : 0);
    return () => clearTimeout(timer);
  }, [watching, job, poll.invoke]);
  // Stop when the page unmounts: a timer left behind would keep polling for a
  // job nobody is reading.
  useEffect(() => () => setWatching(false), []);

  const failed = job?.status === 'failed' || submit.status === 'failed';
  const running = watching && (submit.status === 'pending' || !job || job.running);
  return {
    value: job?.result ?? undefined,
    job,
    status: running ? 'pending' : failed ? 'failed' : job ? 'succeeded' : submit.status,
    available: submit.available,
    matched: submit.matched,
    error: submit.error || job?.error || '',
    invoke,
  };
}
