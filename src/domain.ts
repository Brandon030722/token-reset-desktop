export type EventStatus = 'watching' | 'promised' | 'confirmed' | 'retracted';
export type EvidenceKind = 'hint' | 'promise' | 'confirmation' | 'context' | 'correction';
export interface Evidence {
  id: string; eventId: string; author: string; postedAt: string;
  text: string; summary: string; url: string; kind: EvidenceKind;
}
export interface ResetEvent {
  id: string; title: string; type: 'global-reset' | 'credit-grant' | 'limited-reset';
  status: EventStatus; scope: string; announcedAt?: string; expectedAt?: string;
  confirmedAt?: string; reviewRequired?: boolean; evidenceIds: string[];
}
export interface Forecast {
  id: string; eventId: string; probability24h: number; probability48h: number;
  generatedAt: string; validUntil: string; windowEndsAt: string;
  // Legacy field names retain schema-v1 compatibility. Values are ordinal
  // signal scores, never calibrated probabilities.
  status: EventStatus; summary: string; method: 'rules-v1' | 'rules-v2' | 'reviewed'; evidenceIds: string[];
  scoreType?: 'ordinal-rule-score'; calibrated?: false;
}
export interface Snapshot {
  version: 1; mode: 'demo' | 'live'; checkedAt: string;
  forecast: Forecast | null; events: ResetEvent[]; evidence: Evidence[];
  history: { at: string; probability24h: number; probability48h: number; eventId: string }[];
}
export const THRESHOLD = 80;
export const MAX_FRESH_MS = 60 * 60 * 1000;
export function isFresh(snapshot: Snapshot, now = Date.now()) {
  const checked = Date.parse(snapshot.checkedAt);
  return Number.isFinite(checked) && now - checked <= MAX_FRESH_MS && checked <= now;
}
export function canAlert(snapshot: Snapshot, now = Date.now()): boolean {
  const f = snapshot.forecast;
  return snapshot.mode === 'live' && isFresh(snapshot, now) && !!f &&
    f.method === 'rules-v2' && f.probability48h >= THRESHOLD && f.probability48h <= 100 && f.status === 'promised' &&
    Date.parse(f.generatedAt) <= now && now - Date.parse(f.generatedAt) <= MAX_FRESH_MS &&
    Date.parse(f.validUntil) > now && Date.parse(f.windowEndsAt) > now &&
    Date.parse(f.validUntil) <= Date.parse(f.generatedAt) + MAX_FRESH_MS &&
    new Set(f.evidenceIds).size === f.evidenceIds.length && f.evidenceIds.length > 0 && snapshot.events.some(e => e.id === f.eventId && e.type === 'global-reset' && e.status === f.status && !e.reviewRequired) &&
    f.evidenceIds.every(id => snapshot.evidence.some(e => e.id === id && e.eventId === f.eventId && isSourceUrl(e.url) && Date.parse(e.postedAt) <= now)) &&
    snapshot.evidence.some(e => f.evidenceIds.includes(e.id) && e.kind === 'promise') &&
    !snapshot.evidence.some(e => f.evidenceIds.includes(e.id) && ['correction', 'confirmation'].includes(e.kind));
}
export function isSourceUrl(value: string) {
  try {
    const u = new URL(value);
    return u.protocol === 'https:' && !u.username && !u.password &&
      u.hostname === 'x.com' && !u.port && !u.search && !u.hash && /^\/thsottiaux\/status\/\d+$/.test(u.pathname);
  } catch { return false; }
}
export function parseSnapshot(input: unknown, now = Date.now()): Snapshot {
  const s = input as Snapshot;
  const fail = () => { throw new Error('Invalid snapshot'); };
  const str = (v: unknown, max = 3000): v is string => typeof v === 'string' && v.length > 0 && v.length <= max;
  const id = (v: unknown) => str(v, 100) && /^[\w-]+$/.test(v);
  const date = (v: unknown) => str(v, 40) && Number.isFinite(Date.parse(v));
  const past = (v: unknown) => date(v) && Date.parse(v as string) <= now + 300_000;
  const prob = (v: unknown) => typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 100;
  const status = (v: unknown) => ['watching', 'promised', 'confirmed', 'retracted'].includes(String(v));
  const ids = (v: unknown): v is string[] => Array.isArray(v) && v.length <= 100 && v.every(id) && new Set(v).size === v.length;
  if (!s || s.version !== 1 || !['demo', 'live'].includes(s.mode) || !past(s.checkedAt) ||
    !Array.isArray(s.events) || s.events.length > 200 || !Array.isArray(s.evidence) || s.evidence.length > 500 ||
    !Array.isArray(s.history) || s.history.length > 1000) fail();
  if (new Set(s.events.map(e => e.id)).size !== s.events.length || new Set(s.evidence.map(e => e.id)).size !== s.evidence.length) fail();
  for (const e of s.evidence) {
    if (!id(e.id) || !id(e.eventId) || !str(e.author, 80) || !past(e.postedAt) || !str(e.text, 12000) || !str(e.summary) ||
      !['hint', 'promise', 'confirmation', 'context', 'correction'].includes(e.kind) || !isSourceUrl(e.url)) fail();
  }
  for (const e of s.events) {
    if (!id(e.id) || !str(e.title, 240) || !str(e.scope, 500) || !status(e.status) || !['global-reset', 'credit-grant', 'limited-reset'].includes(e.type) || !ids(e.evidenceIds)) fail();
    for (const d of [e.announcedAt, e.expectedAt, e.confirmedAt]) if (d && !date(d)) fail();
    if (!e.evidenceIds.every(eid => s.evidence.some(x => x.id === eid && x.eventId === e.id))) fail();
  }
  if (s.forecast) {
    const f = s.forecast;
    if (!id(f.id) || !id(f.eventId) || !prob(f.probability24h) || !prob(f.probability48h) || f.probability24h > f.probability48h ||
      !past(f.generatedAt) || !date(f.validUntil) || !date(f.windowEndsAt) || !status(f.status) || !str(f.summary) ||
      !['rules-v1', 'rules-v2', 'reviewed'].includes(f.method) || !ids(f.evidenceIds) ||
      Date.parse(f.validUntil) > Date.parse(f.generatedAt) + MAX_FRESH_MS ||
      Date.parse(f.validUntil) <= Date.parse(f.generatedAt) || Date.parse(f.windowEndsAt) <= Date.parse(f.generatedAt) ||
      Date.parse(f.windowEndsAt) > Date.parse(f.generatedAt) + 48 * 3600_000 ||
      !s.events.some(e => e.id === f.eventId && e.status === f.status && !e.reviewRequired) ||
      !f.evidenceIds.every(eid => s.evidence.some(e => e.id === eid && e.eventId === f.eventId))) fail();
  }
  for (const h of s.history) if (!past(h.at) || !prob(h.probability24h) || !prob(h.probability48h) || h.probability24h > h.probability48h || !id(h.eventId)) fail();
  return s;
}
export const statusLabels: Record<EventStatus, string> = { watching: '观察中', promised: '已承诺 · 待生效', confirmed: '已确认完成', retracted: '已撤回' };
