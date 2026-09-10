import type { Snapshot } from './domain';
// Fixed, visibly labelled fixtures. Never published by the monitor or eligible for email.
export const demoSnapshot: Snapshot = {
  version: 1, mode: 'demo', checkedAt: '2026-09-07T17:00:00Z',
  forecast: {
    id: 'demo-forecast', eventId: 'demo-event', probability24h: 68, probability48h: 84,
    generatedAt: '2026-09-07T17:00:00Z', validUntil: '2026-09-07T18:00:00Z', windowEndsAt: '2026-09-09T17:00:00Z',
    status: 'promised', method: 'rules-v1', summary: '示例：出现明确的重置预告，正在等待生效确认。预测只表达对公开信息的解读。',
    evidenceIds: ['demo-source-1'],
  },
  events: [
    { id: 'demo-event', title: '一次新的额度重置', type: 'global-reset', status: 'promised', scope: '付费用户；具体范围以公告为准', announcedAt: '2026-09-07T15:20:00Z', expectedAt: '2026-09-08T01:00:00Z', evidenceIds: ['demo-source-1', 'demo-source-2'] },
    { id: 'demo-history', title: '上一轮重置已完成', type: 'global-reset', status: 'confirmed', scope: '付费 Codex 用户', announcedAt: '2026-08-30T17:00:00Z', confirmedAt: '2026-08-31T02:34:00Z', evidenceIds: ['demo-source-3'] },
  ],
  evidence: [
    { id: 'demo-source-1', eventId: 'demo-event', author: 'Tibo', postedAt: '2026-09-07T15:20:00Z', text: '演示摘要：已宣布将进行一次面向付费用户的额度重置，预计稍后生效。此段是界面示例，不是原帖引用。', summary: '明确提到将进行一次重置', url: 'https://x.com/thsottiaux/status/2097043464538264003', kind: 'promise' },
    { id: 'demo-source-2', eventId: 'demo-event', author: 'Tibo', postedAt: '2026-09-07T16:40:00Z', text: '演示摘要：同一事件的后续信息被归并到这张事件卡；不会再次记为一次重置。', summary: '后续信息合并到同一事件', url: 'https://x.com/thsottiaux/status/2097174560412246215', kind: 'context' },
    { id: 'demo-source-3', eventId: 'demo-history', author: 'Tibo', postedAt: '2026-08-31T02:34:00Z', text: '演示摘要：官方发布重置完成的消息。仅用于展示归档后的页面状态。', summary: '额度重置完成', url: 'https://x.com/thsottiaux/status/2094252447271366730', kind: 'confirmation' },
  ],
  history: [24, 24, 25, 26, 26, 31, 34, 34, 45, 58, 72, 84].map((p, i) => ({ at: new Date(Date.UTC(2026, 8, 7, 6 + i)).toISOString(), probability24h: Math.max(12, p - 16), probability48h: p, eventId: 'demo-event' })),
};
