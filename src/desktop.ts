export interface DesktopStatus {
  status: 'running' | 'ok' | 'cooldown' | 'source-unavailable' | 'failed' | 'paused';
  posts?: number;
  nextCheckAt?: string;
  notification?: string;
}
declare global {
  interface Window {
    __TIBO_DESKTOP__?: { platform: 'macos' | 'windows' };
    __TIBO_DESKTOP_STATUS__?: DesktopStatus;
    webkit?: { messageHandlers?: { tibo?: { postMessage: (value: unknown) => void } } };
    chrome?: { webview?: { postMessage: (value: unknown) => void } };
  }
}
export const isDesktop = Boolean(window.__TIBO_DESKTOP__);
export function saveDesktopPreference(key: string, value: string): void {
  if (window.__TIBO_DESKTOP__?.platform === 'macos') {
    window.webkit?.messageHandlers?.tibo?.postMessage({ type: 'preferences.save', key, value });
  }
}
export function checkOnDesktop(): boolean {
  const message = { type: 'monitor.check' };
  if (window.__TIBO_DESKTOP__?.platform === 'macos' && window.webkit?.messageHandlers?.tibo) {
    window.webkit.messageHandlers.tibo.postMessage(message);
    return true;
  }
  if (window.__TIBO_DESKTOP__?.platform === 'windows' && window.chrome?.webview) {
    window.chrome.webview.postMessage(message);
    return true;
  }
  return false;
}
export function describeDesktopStatus(detail: DesktopStatus): string {
  switch (detail.status) {
    case 'running': return '正在检查公开动态…';
    case 'paused': return '监控已暂停 · 右键菜单栏图标可恢复';
    case 'cooldown': {
      const next = detail.nextCheckAt ? Date.parse(detail.nextCheckAt) : NaN;
      const label = Number.isFinite(next) ? new Date(next).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : '';
      return label ? '本轮已检查，下次可检查时间 ' + label : '本轮已检查，15 分钟内不重复请求';
    }
    case 'ok': return '已检查 ' + (detail.posts ?? 0) + ' 条动态 · ' + (detail.notification === 'submitted' ? '邮件已提交' : ({ 'needs-review': '邮件状态待核查', 'mail-failed': '邮件连接失败，请检查配置', 'quota-limited': '邮件额度不足，暂未发送', 'audience-limit': '订阅人数超过发送上限', 'empty-list': '暂无已确认订阅者', 'free-plan-unverified': '未能核实免费邮件额度', 'creating': '邮件创建状态待核查', 'created': '邮件草稿待核查', 'scheduling': '邮件提交结果待核查' }[detail.notification ?? ''] ?? '本次检查完成'));
    case 'source-unavailable': return '来源暂时不可用，保留历史记录';
    case 'failed': return '本次检查失败，请通过应用菜单核对配置';
  }
}

export function notificationOnDesktop(action: 'authorize' | 'test'): boolean {
  const bridge = window.webkit?.messageHandlers?.tibo;
  if (window.__TIBO_DESKTOP__?.platform !== 'macos' || !bridge) return false;
  bridge.postMessage({ type: 'notification.' + action });
  return true;
}

export function weeklyEmailOnDesktop(enabled: boolean): boolean {
  const bridge = window.webkit?.messageHandlers?.tibo;
  if (window.__TIBO_DESKTOP__?.platform !== 'macos' || !bridge) return false;
  bridge.postMessage({ type: 'weekly.email', enabled });
  return true;
}

export function emailOnDesktop(message: { type: 'email.invite.verify'; code: string } | { type: 'email.configure'; subscriptionUrl: string } | { type: 'email.subscribe' }): boolean {
  const bridge = window.webkit?.messageHandlers?.tibo;
  if (window.__TIBO_DESKTOP__?.platform !== 'macos' || !bridge) return false;
  bridge.postMessage(message);
  return true;
}
