// Slack beyond the run's kill bound before we stop polling and surface a timeout.
export const RUN_POLL_MARGIN_MS = 30_000;

/**
 * How long run()'s poll may wait for a backgrounded run, in ms - `null` for no
 * limit.
 *
 * An unset or `0` timeout is unbounded server-side, so the poll is unbounded
 * too: giving up at a client-side deadline the caller never asked for would
 * abandon a run that is still going.
 */
export function runPollBudgetMs(timeoutSecs?: number | null): number | null {
  if (typeof timeoutSecs !== "number" || timeoutSecs <= 0) return null;
  return timeoutSecs * 1000 + RUN_POLL_MARGIN_MS;
}
