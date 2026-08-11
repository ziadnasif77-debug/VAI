/**
 * Polling for screens that watch work happening (SPEC §60).
 *
 * The analysis and render screens show a pipeline in motion, and the backend
 * has no push channel — it is a local process running long jobs, and adding
 * websockets for a progress bar would be more machinery than the problem
 * deserves.
 *
 * Two rules keep polling honest:
 *
 * **It stops when there is nothing to watch.** A finished pipeline is polled
 * once more and then left alone; a screen that keeps asking forever is how a
 * local app ends up with a busy CPU while idle.
 *
 * **A failed request does not stop the loop.** The backend restarting mid-poll
 * is normal during development, and a fetch error should show as a stale value,
 * not a dead screen.
 */

import {useCallback, useEffect, useRef, useState} from 'react';

export type PollState<T> = {
  data: T | null;
  error: string | null;
  loading: boolean;
  /** Force an immediate fetch, e.g. right after an action. */
  refresh: () => void;
};

export function usePolling<T>(
  fetcher: () => Promise<T>,
  options: {intervalMs: number; active?: (value: T | null) => boolean},
): PollState<T> {
  const {intervalMs, active} = options;
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // The fetcher is usually an inline arrow, so a ref keeps the effect from
  // re-subscribing on every render and hammering the backend.
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const activeRef = useRef(active);
  activeRef.current = active;

  const [tick, setTick] = useState(0);
  const refresh = useCallback(() => setTick((value) => value + 1), []);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;

    const run = async () => {
      try {
        const value = await fetcherRef.current();
        if (cancelled) return;
        setData(value);
        setError(null);
        const keepGoing = activeRef.current ? activeRef.current(value) : true;
        if (keepGoing) {
          timer = window.setTimeout(run, intervalMs);
        }
      } catch (failure) {
        if (cancelled) return;
        setError(failure instanceof Error ? failure.message : String(failure));
        // Keep trying: a backend restart is not a reason to give up.
        timer = window.setTimeout(run, intervalMs);
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    void run();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [intervalMs, tick]);

  return {data, error, loading, refresh};
}
