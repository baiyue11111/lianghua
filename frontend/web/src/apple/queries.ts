import type {Run} from '@display/research/port';

// Every screen that lists strategies reads the same window of the archive, and
// it has to be the *same* request: two screens asking with different limits
// would hold two different answers to the same question.
export const ARCHIVE_WINDOW = 1000;

export const ARCHIVE_QUERY = {limit: ARCHIVE_WINDOW};

/** The archive returned fewer rows than it holds: every aggregate below is a
 *  statement about the window, not about the archive. */
export function truncated(value?: {items: Run[]; total?: number}): number {
  const held = value?.total ?? value?.items.length ?? 0;
  return held > (value?.items.length ?? 0) ? held : 0;
}

export function statusMix(runs: Run[]) {
  const count = (pattern: RegExp) => runs.filter(run => pattern.test(run.status)).length;
  return {
    done: count(/complete|succeed|success/i),
    running: count(/running|pending/i),
    failed: count(/fail|error/i),
  };
}
