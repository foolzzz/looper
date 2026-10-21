/** Polls politely: `tick` runs at once, and again `intervalMs` after the previous one settled (never two at a time).
 * It waits while the tab is hidden and runs again as soon as the tab is visible. `tick` resolving true ends the
 * polling; the returned function ends it too and aborts the `tick` in flight (its `signal`). */
export function startPolling(
  tick: (signal: AbortSignal) => Promise<boolean | void>,
  intervalMs: number,
): () => void {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let inFlight = false;
  let stopped = false;

  const stop = () => {
    stopped = true;
    clearTimeout(timer);
    document.removeEventListener("visibilitychange", onVisibility);
    controller.abort();
  };

  const run = async () => {
    clearTimeout(timer);
    if (stopped || inFlight || document.visibilityState === "hidden") return;
    inFlight = true;
    let done = false;
    try {
      done = (await tick(controller.signal)) === true;
    } catch {
      // The tick reports its own errors; a failed one is tried again.
    }
    inFlight = false;
    if (stopped) return;
    if (done) stop();
    else timer = setTimeout(run, intervalMs);
  };

  function onVisibility() {
    if (document.visibilityState === "visible") void run();
  }

  document.addEventListener("visibilitychange", onVisibility);
  void run();
  return stop;
}
