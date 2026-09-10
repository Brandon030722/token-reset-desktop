import { useCallback, useEffect, useRef, useState } from 'react';
import { emailOnDesktop, weeklyEmailOnDesktop, notificationOnDesktop, checkOnDesktop, describeDesktopStatus, type DesktopStatus } from './desktop';
import { demoSnapshot } from './demo';
import { isFresh, MAX_FRESH_MS, parseSnapshot, statusLabels, type Snapshot } from './domain';
import { parseWeekly, weeklyFresh, type WeeklyUsage } from './weekly';
import './desktop.css';

interface Health {
  status: 'ok' | 'unavailable';
  source?: string;
  posts?: number;
  attemptedAt?: string;
}

const statuses = ['running', 'ok', 'cooldown', 'source-unavailable', 'failed', 'paused'];
const isStatus = (value: unknown): value is DesktopStatus => !!value && typeof value === 'object' &&
  'status' in value && typeof value.status === 'string' && statuses.includes(value.status);
const formatTime = (value?: string) => value && Number.isFinite(Date.parse(value))
  ? new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value))
  : '尚未成功';

function Icon({ name }: { name: 'refresh' | 'external' | 'arrow' }) {
  const paths = {
    refresh: 'M20 7a9 9 0 1 0 1 9M20 3v5h-5',
    external: 'M14 3h7v7m0-7L10 14M10 3H3v18h18v-7',
    arrow: 'M4 12h16m-6-6 6 6-6 6',
  };
  return <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name]} /></svg>;
}

export default function DesktopApp() {
  const [tab, setTab] = useState<'overview' | 'activity' | 'weekly' | 'reminders'>('overview');
  const [weekly, setWeekly] = useState<WeeklyUsage | null>(null);
  const [email, setEmail] = useState('');
  const [mailBusy, setMailBusy] = useState(false);
  const [inviteCode, setInviteCode] = useState('');
  const [subscriptionStatus, setSubscriptionStatus] = useState('none');
  const [subscriptionConfigured, setSubscriptionConfigured] = useState(false);
  const [mailMessage, setMailMessage] = useState('');
  const [noticeMessage, setNoticeMessage] = useState('');
  const [permission, setPermission] = useState<boolean | null>(null);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [demo, setDemo] = useState(false);
  const [reading, setReading] = useState(true);
  const [readFailed, setReadFailed] = useState(false);
  const [native, setNative] = useState<DesktopStatus | null>(() =>
    isStatus(window.__TIBO_DESKTOP_STATUS__) ? window.__TIBO_DESKTOP_STATUS__ : null);
  const [postCount, setPostCount] = useState<number | null>(null);
  const [now, setNow] = useState(Date.now());
  const sequence = useRef(0);
  const loadedLive = useRef(false);
  const isMac = window.__TIBO_DESKTOP__?.platform === 'macos';
  const busy = native?.status === 'running';

  const readData = useCallback(async () => {
    const current = ++sequence.current;
    setReading(true);
    const stamp = '?t=' + Date.now();
    const fetchJson = async (path: string) => {
      const response = await fetch(import.meta.env.BASE_URL + path + stamp, { cache: 'no-store' });
      if (!response.ok) throw new Error('Local data unavailable');
      return response.json() as Promise<unknown>;
    };
    const [healthResult, snapshotResult, weeklyResult] = await Promise.allSettled([
      fetchJson('data/health.json'), fetchJson('data/snapshot.json'), fetchJson('codex-usage.json'),
    ]);
    if (current !== sequence.current) return;
    if (weeklyResult.status === 'fulfilled') {
      try { setWeekly(parseWeekly(weeklyResult.value)); } catch { setWeekly(null); }
    } else setWeekly(null);
    let validHealth = false;
    let validSnapshot = false;
    if (healthResult.status === 'fulfilled') {
      const value = healthResult.value as Partial<Health> | null;
      if (value && (value.status === 'ok' || value.status === 'unavailable')) {
        setHealth({ status: value.status, attemptedAt: value.attemptedAt, source: typeof value.source === 'string' ? value.source : undefined });
        if (value.status === 'ok' && Number.isInteger(value.posts) && value.posts! >= 0)
          setPostCount(value.posts!);
        validHealth = true;
      }
    }
    if (!validHealth) setHealth(null);
    if (snapshotResult.status === 'fulfilled') {
      try {
        const value = parseSnapshot(snapshotResult.value);
        if (value.mode !== 'live') throw new Error('Expected live data');
        setSnapshot(value);
        if (!loadedLive.current) setDemo(false);
        loadedLive.current = true;
        validSnapshot = true;
      } catch { /* Keep the previous snapshot available as historical evidence. */ }
    }
    setReadFailed(!validHealth || !validSnapshot);
    setReading(false);
    setNow(Date.now());
  }, []);

  useEffect(() => {
    let active = true;
    const applyEmailState = (config: Record<string, unknown>) => {
      if (!active) return;
      setSubscriptionStatus(typeof config.subscriptionStatus === 'string' ? config.subscriptionStatus : 'none');
      setMailBusy(false);
      if (typeof config.email === 'string' && config.email) setEmail(config.email);
      setSubscriptionConfigured(config.subscriptionConfigured === true);
      if (typeof config.message === 'string') setMailMessage(config.message);
      if (config.subscriptionStatus === 'pending' || config.subscriptionStatus === 'active') setInviteCode('');
    };
    fetch(import.meta.env.BASE_URL + 'config.json', { cache: 'no-store' }).then(r => r.json()).then(applyEmailState).catch(() => {});
    const onEmail = (event: Event) => applyEmailState((event as CustomEvent<Record<string, unknown>>).detail ?? {});
    window.addEventListener('tibo:email', onEmail);
    const readNotification = () => {
      if (!isMac) return;
      fetch(import.meta.env.BASE_URL + 'notification-status.json', { cache: 'no-store' }).then(r => r.json()).then(value => {
        if (!active) return;
        if (typeof value.permissionGranted === 'boolean') { setPermission(value.permissionGranted); setNoticeMessage(value.permissionGranted ? '已获系统授权，横幅显示仍取决于系统设置。' : '请在系统设置中允许通知。'); }
        if (value.testInNotificationCenter === true) setNoticeMessage('测试通知已进入通知中心。是否弹出横幅由系统设置决定。');
        else if (value.testAccepted === true) setNoticeMessage('测试通知已提交给系统，正在确认投递状态。');
        else if (value.testAccepted === false) setNoticeMessage('系统未接受测试通知，请检查通知权限。');
      }).catch(() => {});
    };
    readNotification();
    window.addEventListener('tibo:notification', readNotification);
    return () => { active = false; window.removeEventListener('tibo:notification', readNotification); window.removeEventListener('tibo:email', onEmail); };
  }, [isMac]);

  function mailAction(action: 'subscribe' | 'status' | 'cancel') {
    const message = action === 'subscribe' ? { type: 'email.subscribe' as const, email, code: inviteCode } : { type: ('email.' + action) as 'email.status' | 'email.cancel' };
    setMailBusy(true); setMailMessage('正在连接邮件服务…');
    if (!emailOnDesktop(message)) { setMailBusy(false); setMailMessage('请在 macOS 独立应用中操作。'); }
  }
  useEffect(() => { if (!mailBusy) return; const timeout = window.setTimeout(() => { setMailBusy(false); setMailMessage('连接超时，请检查状态后重试。'); }, 35_000); return () => clearTimeout(timeout); }, [mailBusy]);

  function notify(action: 'authorize' | 'test') {
    setNoticeMessage(notificationOnDesktop(action) ? (action === 'test' ? '正在发送测试通知…' : '正在检查系统授权…') : '请在 macOS 独立应用中操作。');
  }

  useEffect(() => {
    document.title = 'Token重置';
    const onMonitor = (event: Event) => {
      const detail = (event as CustomEvent<unknown>).detail;
      if (!isStatus(detail)) return;
      setNative(detail);
      if (detail.status === 'ok' && Number.isInteger(detail.posts) && detail.posts! >= 0)
        setPostCount(detail.posts!);
      if (detail.status !== 'running') void readData();
    };
    const onVisible = () => {
      if (!document.hidden) { setNow(Date.now()); void readData(); }
    };
    window.addEventListener('tibo:monitor', onMonitor);
    document.addEventListener('visibilitychange', onVisible);
    if (isStatus(window.__TIBO_DESKTOP_STATUS__))
      onMonitor(new CustomEvent('tibo:monitor', { detail: window.__TIBO_DESKTOP_STATUS__ }));
    void readData();
    const clock = window.setInterval(() => { if (!document.hidden) setNow(Date.now()); }, 60_000);
    return () => {
      ++sequence.current;
      window.removeEventListener('tibo:monitor', onMonitor);
      document.removeEventListener('visibilitychange', onVisible);
      window.clearInterval(clock);
    };
  }, [readData]);

  useEffect(() => {
    if (!busy) return;
    // Native checks time out first; recover the control if the bridge loses its completion event.
    const timeout = window.setTimeout(() => setNative({ status: 'failed' }), 245_000);
    return () => window.clearTimeout(timeout);
  }, [busy]);

  function check() {
    try {
      setNative(checkOnDesktop() ? { status: 'running' } : { status: 'failed' });
    } catch { setNative({ status: 'failed' }); }
  }

  const data = demo ? demoSnapshot : snapshot;
  const forecast = data?.forecast;
  const event = data?.events.find(item => item.id === forecast?.eventId);
  const sourceFailed = health?.status === 'unavailable' ||
    native?.status === 'source-unavailable' || native?.status === 'failed';
  const liveFresh = !!snapshot && isFresh(snapshot, now) && health?.status === 'ok' && health.attemptedAt === snapshot.checkedAt && !readFailed && !sourceFailed;
  const validForecast = !!forecast && !!event && !event.reviewRequired &&
    ['watching', 'promised'].includes(forecast.status) && event.status === forecast.status &&
    forecast.evidenceIds.length > 0 &&
    (demo || (liveFresh && forecast.method === 'rules-v2' && Date.parse(forecast.validUntil) > now &&
      Date.parse(forecast.windowEndsAt) > now && Date.parse(forecast.generatedAt) <= now &&
      now - Date.parse(forecast.generatedAt) <= MAX_FRESH_MS));
  const probability = validForecast ? forecast!.probability48h : null;
  const activities = [...(data?.events ?? [])].sort((a, b) => Date.parse(b.announcedAt ?? '') - Date.parse(a.announcedAt ?? ''));
  const waiting = !snapshot;
  const title = probability !== null ? (probability >= 80 ? '出现明确重置线索' : '还需要更多线索')
    : sourceFailed ? '暂时无法判断'
    : waiting ? (busy || reading ? '正在读取动态' : '等待首次检查')
    : !liveFresh ? '等待新数据'
    : '暂无广泛重置预告';
  const explanation = probability !== null ? '信号评分不是发生概率；明确承诺、范围和时间共同决定等级。'
    : sourceFailed ? '本轮未取得有效结果，暂停显示当前估计。'
    : waiting ? '取得有效公开动态后，这里会显示判断结果。'
    : !liveFresh ? '上次数据已过期或暂不可读取，保留历史记录。'
    : activities.length ? '已读到其他重置动态，可切换到「动态」查看。'
    : '已读取公开动态，暂未发现符合条件的广泛重置预告。';
  const collection = busy ? '正在检查' : native?.status === 'paused' ? '监控已暂停'
    : sourceFailed ? '采集暂不可用' : waiting ? '等待采集'
    : readFailed ? '数据读取失败' : !liveFresh ? '数据已过期' : '采集正常';
  const nativeNote = native && (!['running', 'ok'].includes(native.status) || ['mail-failed', 'needs-review', 'quota-limited', 'audience-limit', 'free-plan-unverified', 'empty-list'].includes(native.notification ?? '')) ? describeDesktopStatus(native) : '';

  const tabs = [{ id: 'overview', label: '概览' }, { id: 'activity', label: '动态' }, { id: 'weekly', label: '我的额度' }, { id: 'reminders', label: '提醒' }] as const;
  function selectTab(id: typeof tab) { setTab(id); }

  return <div className="token-panel" data-ark-theme="popucom" data-ark-depth="moderate">
    <header className="token-header">
      <div className="token-brand"><span className="brand-mark" aria-hidden="true">T<span>!</span></span><h1>Token重置</h1></div>
      <button className="token-mail-shortcut" onClick={() => selectTab('reminders')}>邮件提醒 <Icon name="arrow" /></button>
    </header>
    <div className="token-tabs" role="tablist" aria-label="观察站页面">
      {tabs.map((item, index) => <button key={item.id} id={'tab-' + item.id} role="tab" aria-selected={tab === item.id}
        aria-controls={'panel-' + item.id} tabIndex={tab === item.id ? 0 : -1} onClick={() => selectTab(item.id)}
        onKeyDown={e => {
          const next = e.key === 'ArrowRight' ? (index + 1) % tabs.length : e.key === 'ArrowLeft' ? (index + tabs.length - 1) % tabs.length : e.key === 'Home' ? 0 : e.key === 'End' ? tabs.length - 1 : -1;
          if (next < 0) return;
          e.preventDefault(); selectTab(tabs[next].id); document.getElementById('tab-' + tabs[next].id)?.focus();
        }}>{item.label}{item.id === 'activity' && activities.length > 0 && <span>{activities.length}</span>}</button>)}
    </div>
    <main className="token-main">
      {demo && <p className="token-demo-notice" role="status">演示数据，不参与通知。</p>}
      <section role="tabpanel" id="panel-overview" aria-labelledby="tab-overview" hidden={tab !== 'overview'}>
        <div className="token-result">
          <p className="token-label">重置信号 · 观察上限 48 小时</p>
          {probability !== null && <p className="token-value">{probability}<span>/100</span></p>}
          <h2 className="token-result-title">{title}</h2>
          <p className="token-explanation">{explanation}</p>
        </div>
        {activities[0] && <button className="token-latest" onClick={() => selectTab('activity')}><span><small>最新动态</small><strong>{activities[0].title}</strong></span><Icon name="arrow" /></button>}
        <div className="token-collection">
          <div className="token-check-row"><p className="token-collection-state" role="status"><span className={'token-status-dot' + (sourceFailed || readFailed ? ' token-warning' : '')} />{collection}</p><button className="token-check-button" onClick={check} disabled={busy || native?.status === 'paused'}><Icon name="refresh" />{busy ? '检查中' : '立即检查'}</button></div>
          <p className="token-metadata">最近成功 {formatTime(snapshot?.checkedAt)} · {postCount === null ? '—' : postCount} 条动态</p>
          {nativeNote && <p className="token-native-note">{nativeNote}</p>}
        </div>
      </section>
      <section role="tabpanel" id="panel-activity" aria-labelledby="tab-activity" hidden={tab !== 'activity'}>
        <div className="token-section-heading"><h2>重置动态</h2><span>@thsottiaux</span></div>
        {!demo && !liveFresh && <p className="token-muted">历史记录，等待采集更新。</p>}
        {activities.length === 0 && <p className="token-empty">暂未收录重置动态。</p>}
        <div className="token-announcements">{activities.map(item => {
          const sources = data!.evidence.filter(source => item.evidenceIds.includes(source.id) && source.eventId === item.id);
          return <article key={item.id}>
            <div className="token-announcement-meta"><span>{item.type === 'limited-reset' ? '限定人群' : '广泛重置线索'}</span><time dateTime={item.announcedAt}>{formatTime(item.announcedAt)}</time></div>
            <h3>{item.title}</h3><p>{item.scope}</p>
            <p className="token-muted">{item.status === 'promised' ? '已公告 · 到账请自行核对' : statusLabels[item.status]}</p>
            <details><summary>查看依据与原文</summary>{sources.map(source => <div key={source.id}><p className="token-original">{source.text}</p><a href={source.url} target="_blank" rel="noopener noreferrer">查看原帖 <Icon name="external" /></a></div>)}</details>
          </article>;
        })}</div>
      </section>
      <section role="tabpanel" id="panel-weekly" aria-labelledby="tab-weekly" hidden={tab !== 'weekly'}>
        <div className="token-weekly-heading"><h2>每周额度</h2><span className="token-muted">{weeklyFresh(weekly, now) ? '已连接本机 Codex' : '等待有效读取'}</span></div>
        {!weekly && <p className="token-email-message">安装并登录 Codex 后，点击「立即检查」读取。当前尚无可用的每周额度数据。</p>}
        {weekly && !weeklyFresh(weekly, now) && <p className="token-email-message">当前未取得有效额度。{weekly.windows.length ? '下方为上次记录，暂不确认已恢复。' : '请确认本机 Codex 已登录。'}</p>}
        {weekly?.status === 'ok' && weekly.windows.length === 0 && <p className="token-email-message">接口尚未提供七天额度窗口，暂时无法设置每周提醒。</p>}
        {weekly?.windows.map(w => <article className="token-weekly-card" key={w.id}>
          <div><h3>{w.label}</h3><span>七天周期</span></div>
          <p className="token-weekly-remaining">{weeklyFresh(weekly, now) ? w.remainingPercent.toLocaleString('zh-CN', { maximumFractionDigits: 1 }) + '%' : '—'}<small>本周剩余</small></p>
          <p>服务返回恢复时间 <strong>{formatTime(w.resetsAt)}</strong></p>
          {Date.parse(w.resetsAt) <= now && <p className="token-muted">记录时间已到，等待下一次读取确认。</p>}
        </article>)}
        <div className="token-weekly-mail" data-state={weekly?.cloudMail?.status || 'disabled'}>
          <div className="token-weekly-mail-heading"><h3>个人周邮件</h3><span role="status">{weekly?.cloudMail?.status === 'synced' ? '已同步云端' : weekly?.cloudMail?.status === 'sync-failed' ? '同步失败' : weekly?.cloudMail?.status === 'read-unavailable' ? '待同步' : '未开启'}</span></div>
          {weekly?.cloudMail?.status === 'synced' && weekly.cloudMail.nextAt ? <p className="token-weekly-mail-date"><span>下次提醒</span><time dateTime={weekly.cloudMail.nextAt}>{formatTime(weekly.cloudMail.nextAt)}</time></p> : <p className="token-weekly-mail-note">{weekly?.cloudMail?.status === 'synced' ? '暂无待预约的恢复时间。' : weekly?.cloudMail?.status === 'sync-failed' || weekly?.cloudMail?.status === 'read-unavailable' ? '更新未确认，旧预约可能仍有效。请联网重试。' : weekly?.cloudMail?.configured ? '开启后自动预约到点邮件。' : '配置后可预约个人到点邮件。'}</p>}
          <div className="token-weekly-mail-actions"><span>{weekly?.cloudMail?.status === 'synced' && weekly.cloudMail.nextAt ? '关机也能收到' : '仅发给你的邮箱'}</span>{weekly?.cloudMail?.configured && <button disabled={busy} onClick={() => { if (weeklyEmailOnDesktop(!weekly.cloudMail?.enabled)) setNative({ status: 'running' }); }}>{weekly.cloudMail.enabled ? '关闭周邮件' : '启用周邮件'}</button>}</div>
        </div>
        <div className="token-weekly-tools"><span className="token-muted">最近读取 {formatTime(weekly?.checkedAt)}</span><button className="token-check-button" onClick={check} disabled={busy}> {busy ? '正在检查' : '立即检查'} <Icon name="refresh" /></button></div>
        <details className="token-weekly-details"><summary>提醒说明</summary><p>云端每 15 分钟检查，发信可能延迟；按记录时间提醒，不代表额度已到账。下一周期需本机重新读取；未使用的额度暂不预约。</p><p>仅邮箱和恢复时间同步云端，用量与登录信息留在本机。关闭周邮件需联网同步成功；本机系统通知在关机期间暂停。</p></details>
      </section>
      <section role="tabpanel" id="panel-reminders" aria-labelledby="tab-reminders" hidden={tab !== 'reminders'}>
        <div className="token-reminder-block">
          <div className="token-section-heading"><h2>邮件提醒</h2><span>{subscriptionStatus === 'active' ? '已订阅' : subscriptionStatus === 'pending' ? '待确认邮箱' : '凭邀请开启'}</span></div>
          <p>重要重置信号，由 Token重置 统一发到你的邮箱。</p>
          {subscriptionStatus === 'active' ? <><p className="token-invite-ok">{email}</p><p className="token-muted">个人周邮件可在「我的额度」单独开启。</p><button className="token-check-button" disabled={mailBusy} onClick={() => mailAction('status')}>检查订阅状态</button><details className="token-email-setup"><summary>管理订阅</summary><p className="token-muted">退订将关闭所有邮件及个人预约。</p><button disabled={mailBusy} onClick={() => mailAction('cancel')}>退订邮件</button></details></> : <>
            {subscriptionStatus === 'pending' && <><p className="token-invite-ok">{email} · 请先点击确认邮件</p><button className="token-check-button" disabled={mailBusy} onClick={() => mailAction('status')}>检查确认状态</button></>}
            <form className="token-email-form" onSubmit={e => { e.preventDefault(); mailAction('subscribe'); }}>
              <label htmlFor="email-address">收件邮箱</label><div><input id="email-address" type="email" value={email} onChange={e => setEmail(e.target.value)} placeholder="你希望收到提醒的邮箱" maxLength={254} autoComplete="email" required disabled={mailBusy} /></div>
              <label htmlFor="email-invite">邀请码</label><div><input id="email-invite" type="password" value={inviteCode} onChange={e => setInviteCode(e.target.value)} placeholder="向邀请人获取" maxLength={128} autoComplete="off" spellCheck={false} required disabled={mailBusy} /></div>
              <button type="submit" className="token-check-button" disabled={mailBusy || !subscriptionConfigured}>{mailBusy ? '正在验证…' : '验证并发送确认邮件'}</button>
            </form>
          </>}
          <p className="token-muted">{!subscriptionConfigured ? '邮件服务准备中，看板可照常使用。' : '无需配置发件邮箱或密钥。确认后订阅，可随时退订。'}</p>
          <details className="token-email-setup"><summary>提醒规则</summary><p className="token-muted">广泛重置信号评分 ≥80 分时发信，同一事件一次。由云端监控和发送，关机也能收到。</p></details>
          {mailMessage && <p className="token-feedback" role="status">{mailMessage}</p>}
        </div>
        <div className="token-reminder-block">
          <div className="token-section-heading"><h2>系统通知</h2><span>{!isMac ? '仅 macOS 可用' : permission === null ? '待核对权限' : permission ? '已获授权' : '未获授权'}</span></div>
          <p>广泛重置信号评分 ≥80 分、新的小范围公告，或个人每周恢复时间到达时提醒。</p>
        </div>
        <details className="token-advanced"><summary>高级选项</summary>
          <div className="token-notification-actions"><button onClick={() => notify('authorize')} disabled={!isMac}>检查通知权限</button><button onClick={() => notify('test')} disabled={!isMac}>发送测试通知</button><button onClick={() => setDemo(value => !value)} aria-pressed={demo}>{demo ? '返回实时数据' : '查看演示数据'}</button></div>
          {noticeMessage && <p className="token-feedback" role="status">{noticeMessage}</p>}
        </details>
      </section>
    </main>
    <footer className="token-footer"><p>{isMac ? '每 15 分钟检查 · 关闭面板继续监控' : '每 15 分钟检查 · 关闭窗口后停止'}</p></footer>
  </div>;
}
