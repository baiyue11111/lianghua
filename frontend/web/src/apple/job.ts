import {useEffect, useState} from 'react';
import type {BacktestResult} from '@display/research/port';
import {useOperation} from '../interaction/model';
import {errorMessage, text} from '../shared/format';

export type BacktestJob = {
  job_id: string | null; running: boolean; status: string; phase: string | null;
  trials_done: number; trials_total: number; started_at: string | null;
  finished_at: string | null; error: string | null; code: string | null;
  result: BacktestResult | null;
};

type SubmitReply = {job_id: string};

const POLL_MS = 800;

export function useBacktestJob(target: string, form: unknown) {
  const submit = useOperation<SubmitReply>(target, 'backtest.submit', form, true, false);
  const [id, setId] = useState<string | null>(null);
  const {invoke: read, status: readStatus, value} = useOperation<BacktestJob>(
    target, 'backtest.job', id ? {id} : {}, false, false);
  const lost = id !== null && value !== undefined && value.job_id !== id;
  const job = id && !lost ? value : undefined;

  async function invoke(input?: unknown) {
    setId(null);
    const state = await submit.invoke(input);
    const slot = state.targets?.[target];
    if (!slot?.problems?.length && slot?.operations?.['backtest.submit']?.status === 'succeeded') {
      const reply = (slot.values?.['backtest.submit'] as SubmitReply[] | undefined)?.[0];
      if (reply?.job_id) {
        setId(reply.job_id);
        void read({id: reply.job_id});
      }
    }
    return state;
  }

  useEffect(() => {
    if (!job?.running || readStatus !== 'succeeded') return;
    const timer = setTimeout(() => void read(), POLL_MS);
    return () => clearTimeout(timer);
  }, [job, read, readStatus]);

  const failed = lost || job?.status === 'failed' || submit.status === 'failed'
    || (id !== null && readStatus === 'failed');
  const running = submit.status === 'pending' || (id !== null && !failed && (!job || job.running));
  const error = submit.error ? errorMessage(submit.error)
    : lost ? '找不到这次任务，请在策略存档中查看结果'
    : job?.error ? text(job.error.replace(/^\w+:\s*/, '')) ?? errorMessage(job.error) : '';
  return {
    value: job?.result ?? undefined,
    job,
    status: running ? 'pending' : failed ? 'failed' : job ? 'succeeded' : submit.status,
    error,
    invoke,
  };
}
