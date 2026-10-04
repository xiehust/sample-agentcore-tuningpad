import { useEffect, useRef } from "react";

export const DETAIL_POLL_MS = 2500;
export const LIST_POLL_MS = 8000;

/** Call `fn` every `ms` while `active`; the latest `fn` is always used. */
export function usePoll(fn: () => void, ms: number, active = true): void {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => ref.current(), ms);
    return () => window.clearInterval(id);
  }, [ms, active]);
}
