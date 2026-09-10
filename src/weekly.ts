export interface WeeklyWindow {
  id: string; label: string; windowDurationMins: 10080;
  usedPercent: number; remainingPercent: number; resetsAt: string;
}
export interface WeeklyUsage {
  version: 1; status: 'ok' | 'unavailable'; checkedAt?: string; attemptedAt: string;
  windows: WeeklyWindow[]; reason?: string;
  cloudMail?: { status: 'disabled' | 'synced' | 'sync-failed' | 'read-unavailable'; configured: boolean; enabled: boolean; pending?: number; syncedAt?: string; nextAt?: string | null };
}
export function parseWeekly(value: unknown): WeeklyUsage {
  const v = value as WeeklyUsage;
  const validDate = (x: unknown) => typeof x === 'string' && Number.isFinite(Date.parse(x));
  if (!v || v.version !== 1 || !['ok', 'unavailable'].includes(v.status) || !validDate(v.attemptedAt) ||
    !Array.isArray(v.windows) || v.windows.length > 20 || (v.status === 'ok' && !validDate(v.checkedAt))) throw Error('Invalid weekly data');
  const seen = new Set<string>();
  if (v.cloudMail && (!['disabled','synced','sync-failed','read-unavailable'].includes(v.cloudMail.status) ||
    typeof v.cloudMail.enabled !== 'boolean' || typeof v.cloudMail.configured !== 'boolean' ||
    (v.cloudMail.nextAt != null && !validDate(v.cloudMail.nextAt)))) throw Error('Invalid cloud schedule');
  for (const w of v.windows) {
    if (!w || typeof w.id !== 'string' || !w.id || seen.has(w.id) || typeof w.label !== 'string' || w.label.length > 100 ||
      w.windowDurationMins !== 10080 || !validDate(w.resetsAt) || !Number.isFinite(w.usedPercent) || w.usedPercent < 0 || w.usedPercent > 100 ||
      !Number.isFinite(w.remainingPercent) || Math.abs(w.remainingPercent - (100 - w.usedPercent)) > 0.01) throw Error('Invalid weekly window');
    seen.add(w.id);
  }
  return v;
}
export function weeklyFresh(usage: WeeklyUsage | null, now = Date.now()) {
  const at = Date.parse(usage?.checkedAt || '');
  return usage?.status === 'ok' && Number.isFinite(at) && at <= now && now-at <= 30 * 60_000;
}
