// The rule behind the status function, with no network in it so the tests can drive every branch.
//
// status.json is written by `ledgerbell heartbeat` (the time of the last ledger event, the routine, whether the kill switch is engaged, no account figures).
// The bot runs on weekdays only, so Saturday and Sunday do not count towards "silent for too long": Friday 18:00 to Monday 08:00 is a normal gap.

export const MAX_SILENT_HOURS = 36;
const HOUR = 3600 * 1000;
const DAY = 24 * HOUR;

/** Milliseconds between two instants that fall outside Saturday and Sunday (UTC). */
export function weekdayMs(fromMs, toMs) {
  if (toMs <= fromMs) return 0;
  let total = 0;
  let cursor = fromMs;
  while (cursor < toMs) {
    const dayStart = cursor - (cursor % DAY);
    const dayEnd = Math.min(dayStart + DAY, toMs);
    const weekday = new Date(dayStart).getUTCDay(); // 0 is Sunday, 6 is Saturday
    if (weekday !== 0 && weekday !== 6) total += dayEnd - cursor;
    cursor = dayEnd;
  }
  return total;
}

/**
 * @param {unknown} status the parsed status.json
 * @param {number} nowMs
 * @returns {{code: number, body: object}} 200 for ok and killed, 503 for stale, unknown and invalid, so an uptime monitor alerts on a bot that went quiet
 */
export function evaluate(status, nowMs, maxSilentHours = MAX_SILENT_HOURS) {
  if (!status || typeof status !== "object") return { code: 503, body: { state: "unknown", why: "no status file" } };
  const last = Date.parse(status.last_event_at ?? "");
  const made = Date.parse(status.generated_at ?? "");
  if (Number.isNaN(last) || Number.isNaN(made)) return { code: 503, body: { state: "unknown", why: "no time in the status file" } };
  if (last > nowMs + 5 * 60 * 1000 || made > nowMs + 5 * 60 * 1000) return { code: 503, body: { state: "invalid", why: "a time in the future" } };
  const silentLast = weekdayMs(last, nowMs) / HOUR;
  const silentMade = weekdayMs(made, nowMs) / HOUR;
  const body = {
    last_routine: String(status.last_routine ?? ""),
    last_event_at: status.last_event_at,
    silent_weekday_hours: Math.round(silentLast * 10) / 10,
    kill_engaged: status.kill_engaged === true,
    mode: String(status.mode ?? ""),
  };
  if (silentMade > maxSilentHours) return { code: 503, body: { ...body, state: "stale", why: "the status file was not refreshed" } };
  if (silentLast > maxSilentHours) return { code: 503, body: { ...body, state: "stale", why: "no routine has run" } };
  return { code: 200, body: { ...body, state: body.kill_engaged ? "killed" : "ok" } };
}
