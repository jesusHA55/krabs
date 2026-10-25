'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  snap: null,
  tab: 'overview',
  timer: null,
  refreshMs: 5000,
  connection: 'idle',
};

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------
const esc = (value) =>
  String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));

const currency = () => state.snap?.config?.wallet?.currency || 'USD';

function money(value, digits = 2) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
  return new Intl.NumberFormat(undefined, {
    style: 'currency', currency: currency(),
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  }).format(Number(value));
}

function num(value, digits = 2) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
  return new Intl.NumberFormat(undefined, {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  }).format(Number(value));
}

function qty(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 4 }).format(Number(value));
}

function pct(value, digits = 2) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
  return `${(Number(value) * 100).toFixed(digits)}%`;
}

function signed(value, formatter = money) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
  const text = formatter(Math.abs(Number(value)));
  return `${Number(value) > 0 ? '+' : Number(value) < 0 ? '−' : ''}${text}`;
}

function tone(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n === 0) return '';
  return n > 0 ? 'pos' : 'neg';
}

function timeAgo(iso) {
  if (!iso) return '—';
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return '—';
  const seconds = Math.max(0, (Date.now() - t) / 1000);
  if (seconds < 5) return 'just now';
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function clock(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (!Number.isFinite(d.getTime())) return '—';
  return d.toLocaleString(undefined, {
    month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit',
  });
}

function shortDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (!Number.isFinite(d.getTime())) return '—';
  return d.toLocaleDateString(undefined, { month: 'short', day: '2-digit' });
}

const pill = (text) => {
  const cls = String(text || 'unknown').toLowerCase().replace(/[^a-z_]/g, '');
  return `<span class="pill ${cls}">${esc(text || 'unknown')}</span>`;
};

// ---------------------------------------------------------------------------
// Networking
// ---------------------------------------------------------------------------
function getCookie(name) {
  const prefix = `${name}=`;
  for (const part of document.cookie.split(';')) {
    const value = part.trim();
    if (value.startsWith(prefix)) return decodeURIComponent(value.slice(prefix.length));
  }
  return '';
}

async function load() {
  try {
    const response = await fetch('/api/snapshot', {
      cache: 'no-store',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    });
    if (response.status === 401) {
      window.location.assign('/login');
      return;
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.snap = await response.json();
    state.connection = 'live';
    render();
  } catch (error) {
    state.connection = 'down';
    updateConnection();
    toast(error.message || 'Dashboard is unreachable', 'error');
  }
}

async function postJSON(path, body) {
  const headers = { 'Content-Type': 'application/json', Accept: 'application/json' };
  const csrf = getCookie('krabs_csrf');
  if (csrf) headers['X-Krabs-CSRF'] = csrf;
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers,
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try {
      const payload = await response.json();
      if (payload.error) message = payload.error;
    } catch (_) { /* ignore */ }
    throw new Error(message);
  }
  return response.json();
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------
function render() {
  if (!state.snap) return;
  const snap = state.snap;
  renderHeader(snap);
  renderCards(snap);
  renderChart(snap);
  renderSession(snap);
  renderPearl(snap);
  if (document.activeElement?.id !== 'kill-reason') renderKill(snap);
  renderPositions(snap);
  renderOrders(snap);
  renderFills(snap);
  renderRuns(snap);
  renderReports(snap);
  renderResearch(snap);
  renderRisk(snap);
  renderExecution(snap);
  renderStrategies(snap);
  renderHealth(snap);
  renderWarnings(snap);
  updateConnection();
}

function renderHeader(snap) {
  const mode = snap.heartbeat?.mode || snap.config.mode || 'unknown';
  const badge = $('#mode-badge');
  badge.textContent = mode;
  badge.className = `badge ${String(mode).toLowerCase().replace(/[^a-z-]/g, '')}`;
  if (snap.heartbeat?.halted) {
    badge.classList.add('halted');
  }
  $('#run-id').textContent = snap.heartbeat?.run_id || snap.equity.run_id || '';
  $('#updated').textContent = snap.generated_at ? `updated ${timeAgo(snap.generated_at)}` : '';
}

function updateConnection() {
  const el = $('#connection');
  if (!el) return;
  let kind = state.connection;
  const snap = state.snap;
  if (kind === 'live' && snap) {
    if (snap.heartbeat_stale) kind = 'stale';
    else if (!snap.heartbeat || snap.heartbeat.stopped) kind = 'idle';
  }
  el.className = `conn ${kind}`;
  el.title = {
    live: 'Dashboard connected; trading heartbeat is fresh',
    stale: 'Dashboard connected; trading heartbeat is stale',
    idle: 'Dashboard connected; no running trading session',
    down: 'Dashboard unreachable',
  }[kind] || 'Connection status';
}

function renderCards(snap) {
  const account = snap.account || {};
  const hb = snap.heartbeat || {};
  const equity = account.equity;
  const total = account.total_pnl;
  const day = account.day_pnl;
  const gross = account.gross_exposure ?? hb.gross_exposure;
  const exposure = equity ? Math.abs(gross || 0) / equity : null;
  const status = snap.kill.engaged
    ? 'KILLED'
    : hb.halted
      ? 'HALTED'
      : hb.stopped
        ? 'STOPPED'
        : hb.mode
          ? 'RUNNING'
          : 'IDLE';
  const statusTone = status === 'RUNNING' ? 'pos' : status === 'IDLE' || status === 'STOPPED' ? '' : 'neg';

  const cards = [
    metric('Equity', money(equity), account.source === 'database' ? 'last audit mark' : 'live account', tone(total)),
    metric('Total P&L', signed(total), pct(account.total_return), tone(total)),
    metric('Day P&L', signed(day), pct(account.day_return), tone(day)),
    metric('Cash', money(account.cash), `buying power ${money(account.buying_power)}`),
    metric('Exposure', pct(exposure), `gross ${money(gross)}`, (exposure || 0) > 0.9 ? 'warn' : ''),
    metric('Heartbeat', snap.heartbeat ? timeAgo(snap.heartbeat.ts) : 'no session',
      snap.heartbeat_stale ? 'stale feed' : snap.heartbeat ? `pid ${hb.pid ?? '—'}` : '',
      snap.heartbeat_stale ? 'warn' : ''),
    metric('Status', status, snap.kill.engaged ? snap.kill.reason : hb.halt_reason || '', statusTone),
    metric('Positions', String((snap.positions || []).length),
      `${hb.orders ?? 0} orders · ${hb.fills ?? 0} fills`),
  ];
  $('#cards').innerHTML = cards.join('');
}

function metric(label, value, sub = '', dataTone = '') {
  return `<div class="metric" ${dataTone ? `data-tone="${dataTone}"` : ''}>
    <div class="metric-label">${esc(label)}</div>
    <div class="metric-value">${esc(value)}</div>
    <div class="metric-sub">${esc(sub)}</div>
  </div>`;
}

function renderSession(snap) {
  const hb = snap.heartbeat || {};
  const cfg = snap.config;
  const rows = [
    ['Strategy', cfg.strategy.name + (Object.keys(cfg.strategy.params || {}).length
      ? ` ${JSON.stringify(cfg.strategy.params)}` : '')],
    ['Symbols', (cfg.symbols || []).join(', ') || '—'],
    ['Timeframe', cfg.timeframe],
    ['Data feed', `${cfg.feed.provider}${cfg.feed.offline ? ' (offline)' : ''}`],
    ['Broker', `${cfg.broker.provider}${cfg.broker.paper ? ' paper' : ' live'}`],
    ['Last bar', hb.last_bar ? clock(hb.last_bar) : '—'],
    ['Cycles', hb.cycles ?? '—'],
    ['Bars seen', hb.bars ?? '—'],
    ['Orders / fills', `${hb.orders ?? 0} / ${hb.fills ?? 0}`],
    ['Errors', hb.errors ?? '—'],
    ['Start equity', hb.start_equity != null ? money(hb.start_equity) : '—'],
  ];
  const body = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
  const halt = hb.halted
    ? `<div class="warn-item mt">Risk halt: ${esc(hb.halt_reason || 'halted')}</div>`
    : '';
  $('#session').innerHTML = `<dl class="kv">${body}</dl>${halt}`;
}

function renderPearl(snap) {
  const target = $('#pearl');
  if (!target) return;
  const diagnostics = snap.heartbeat?.strategy_diagnostics || {};
  const ready = Boolean(diagnostics.ready);
  $('#pearl-ready').textContent = diagnostics.prediction_ts
    ? `${ready ? 'ready' : 'warming up'} · ${timeAgo(diagnostics.prediction_ts)}`
    : '';
  if (!Object.keys(diagnostics).length) {
    target.innerHTML = emptyBox(
      'No Pearl diagnostics in the heartbeat. Use a PearlStrategy and restart the trading service.'
    );
    return;
  }
  const signals = diagnostics.signals || {};
  const values = diagnostics.values || {};
  const signalRows = Object.entries(signals).map(([name, side]) => [
    `<span class="symbol">${esc(name)}</span>`,
    side > 0 ? '<span class="side-buy">LONG</span>'
      : side < 0 ? '<span class="side-sell">SHORT</span>'
        : '<span class="muted">FLAT</span>',
  ]);
  const valueRows = [];
  Object.entries(values).forEach(([node, fields]) => {
    if (!fields || typeof fields !== 'object') return;
    Object.entries(fields).forEach(([field, value]) => {
      if (typeof value !== 'number' || !Number.isFinite(value)) return;
      valueRows.push([
        `<span class="mono">${esc(node)}</span>`,
        `<span class="muted mono">${esc(field)}</span>`,
        `<span class="num">${num(value, 4)}</span>`,
      ]);
    });
  });
  target.innerHTML = `
    <div class="health">
      <div class="health-item"><div class="label">State</div>
        <div class="value ${ready ? 'ok' : 'warn'}">${ready ? 'ready' : 'warming up'}</div></div>
      <div class="health-item"><div class="label">Prediction ts</div>
        <div class="value">${esc(clock(diagnostics.prediction_ts))}</div></div>
      <div class="health-item"><div class="label">Signals</div>
        <div class="value">${Object.keys(signals).length}</div></div>
      <div class="health-item"><div class="label">Nodes reported</div>
        <div class="value">${Object.keys(values).length}</div></div>
    </div>
    ${signalRows.length ? `<h3 class="mt">Signals</h3>${table(['Signal', 'Side'], signalRows, 'No signals.')}` : ''}
    ${valueRows.length ? `<h3 class="mt">Model values</h3>${table(['Node', 'Field', 'Value'], valueRows, 'No values.', ['', '', 'num'])}` : ''}`;
}

function renderKill(snap) {
  const target = $('#kill-controls');
  if (!target) return;
  const enabled = Boolean(snap.controls?.enabled);
  if (!enabled) {
    target.innerHTML = `<p class="kill-note">Controls are disabled. Restart the dashboard with
      <span class="mono">--allow-controls</span> to enable the kill switch.</p>`;
    return;
  }
  if (snap.kill.engaged) {
    target.innerHTML = `
      <div class="kill-box">
        <div class="kill-state engaged"><span class="dot"></span> ENGAGED</div>
        <div class="kill-note">${esc(snap.kill.reason || 'No reason recorded')}
          ${snap.kill.modified ? ` · ${esc(timeAgo(snap.kill.modified))}` : ''}</div>
        <button id="kill-clear" class="btn danger" type="button">Clear kill switch</button>
      </div>`;
    $('#kill-clear').addEventListener('click', clearKill);
    return;
  }
  target.innerHTML = `
    <div class="kill-box">
      <div class="kill-state clear"><span class="dot"></span> CLEAR</div>
      <p class="kill-note">Engaging blocks new orders and cancels working orders.
        It does not liquidate positions.</p>
      <div class="kill-form">
        <input id="kill-reason" type="text" maxlength="200" placeholder="Reason (optional)">
        <button id="kill-engage" class="btn danger" type="button">Engage</button>
      </div>
    </div>`;
  $('#kill-engage').addEventListener('click', engageKill);
}

async function engageKill() {
  const reason = ($('#kill-reason')?.value || 'dashboard').trim() || 'dashboard';
  if (!window.confirm('Engage the kill switch? New orders will be blocked and working orders cancelled.')) return;
  try {
    await postJSON('/api/kill', { action: 'engage', reason });
    toast('Kill switch engaged');
    await load();
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function clearKill() {
  if (!window.confirm('Clear the kill switch and allow new orders?')) return;
  try {
    await postJSON('/api/kill', { action: 'clear', reason: 'cleared from dashboard' });
    toast('Kill switch cleared');
    await load();
  } catch (error) {
    toast(error.message, 'error');
  }
}

function renderPositions(snap) {
  const positions = snap.positions || [];
  const totalValue = positions.reduce((sum, p) => sum + Math.abs(p.value || 0), 0);
  $('#positions-value').textContent = positions.length
    ? `${positions.length} · ${money(totalValue)} gross`
    : '';

  if (!positions.length) {
    $('#positions').innerHTML = emptyBox('No open positions in the latest heartbeat.');
    $('#positions-mini').innerHTML = emptyBox('No open positions.');
    return;
  }

  const rows = positions.map((p) => [
    `<span class="symbol">${esc(p.symbol)}</span>`,
    `<span class="num">${qty(p.qty)}</span>`,
    `<span class="num">${money(p.avg, 2)}</span>`,
    `<span class="num">${money(p.last, 2)}</span>`,
    `<span class="num">${money(p.value, 0)}</span>`,
    `<span class="num ${tone(p.unrealized)}">${signed(p.unrealized)}</span>`,
    `<span class="num">${pct(p.weight, 1)}</span>`,
  ]);
  $('#positions').innerHTML = table(
    ['Symbol', 'Qty', 'Avg', 'Last', 'Value', 'Unrealized', 'Weight'],
    rows,
    'No open positions.',
    ['', 'num', 'num', 'num', 'num', 'num', 'num'],
  );
  $('#positions-mini').innerHTML = table(
    ['Symbol', 'Qty', 'Value', 'P&L'],
    positions.slice(0, 5).map((p) => [
      `<span class="symbol">${esc(p.symbol)}</span>`,
      `<span class="num">${qty(p.qty)}</span>`,
      `<span class="num">${money(p.value, 0)}</span>`,
      `<span class="num ${tone(p.unrealized)}">${signed(p.unrealized, (v) => money(v, 0))}</span>`,
    ]),
    'No open positions.',
    ['', 'num', 'num', 'num'],
  );
}

function renderOrders(snap) {
  const orders = snap.orders || [];
  if (!orders.length) {
    $('#orders').innerHTML = emptyBox('No orders recorded for this run.');
    return;
  }
  const rows = orders.map((o) => [
    `<span class="nowrap">${esc(clock(o.ts))}</span>`,
    `<span class="symbol">${esc(o.symbol)}</span>`,
    `<span class="${o.side === 'buy' ? 'side-buy' : 'side-sell'}">${esc(o.side)}</span>`,
    esc(o.type),
    `<span class="num">${qty(o.filled_qty)} / ${qty(o.qty)}</span>`,
    `<span class="num">${o.avg_price ? money(o.avg_price) : '—'}</span>`,
    pill(o.status),
    `<span class="muted">${esc(o.reason || o.tag || '')}</span>`,
  ]);
  $('#orders').innerHTML = table(
    ['Time', 'Symbol', 'Side', 'Type', 'Filled', 'Avg', 'Status', 'Reason'],
    rows, 'No orders.', ['nowrap', '', '', '', 'num', 'num', '', ''],
  );
}

function renderFills(snap) {
  const fills = snap.fills || [];
  if (!fills.length) {
    $('#fills').innerHTML = emptyBox('No fills recorded for this run.');
    return;
  }
  const rows = fills.map((f) => [
    `<span class="nowrap">${esc(clock(f.ts))}</span>`,
    `<span class="symbol">${esc(f.symbol)}</span>`,
    `<span class="${f.side === 'buy' ? 'side-buy' : 'side-sell'}">${esc(f.side)}</span>`,
    `<span class="num">${qty(f.qty)}</span>`,
    `<span class="num">${money(f.price)}</span>`,
    `<span class="num">${money((f.qty || 0) * (f.price || 0), 0)}</span>`,
    `<span class="num">${money(f.commission)}</span>`,
  ]);
  $('#fills').innerHTML = table(
    ['Time', 'Symbol', 'Side', 'Qty', 'Price', 'Notional', 'Commission'],
    rows, 'No fills.', ['nowrap', '', '', 'num', 'num', 'num', 'num'],
  );
}

function renderRuns(snap) {
  const runs = snap.runs || [];
  if (!runs.length) {
    $('#runs').innerHTML = emptyBox('No audit runs yet. Enable [storage] and start a paper/live session.');
    return;
  }
  const rows = runs.map((r) => {
    const ret = r.initial_cash && r.final_equity ? r.final_equity / r.initial_cash - 1 : null;
    return [
      `<span class="nowrap">${esc(clock(r.started_at))}</span>`,
      `<span class="symbol">${esc(r.strategy)}</span>`,
      esc(r.mode),
      pill(r.status),
      `<span class="num">${money(r.initial_cash, 0)}</span>`,
      `<span class="num">${r.final_equity ? money(r.final_equity, 0) : '—'}</span>`,
      `<span class="num ${tone(ret)}">${pct(ret)}</span>`,
      `<span class="muted mono">${esc(r.run_id)}</span>`,
    ];
  });
  $('#runs').innerHTML = table(
    ['Started', 'Strategy', 'Mode', 'Status', 'Initial', 'Final', 'Return', 'Run'],
    rows, 'No runs.', ['nowrap', '', '', '', 'num', 'num', 'num', ''],
  );
}

function renderReports(snap) {
  const reports = snap.reports || [];
  if (!reports.length) {
    $('#reports').innerHTML = emptyBox(
      'No reports found. Run <span class="mono">krabs backtest</span> or <span class="mono">krabs optimize</span>.'
    );
    return;
  }
  const cards = reports.map((r) => {
    const raw = `/api/report?path=${encodeURIComponent(r.path)}`;
    let metrics = '';
    if (r.type === 'backtest') {
      metrics = [
        ['Return', pct(r.return), tone(r.return)],
        ['Sharpe', num(r.sharpe), ''],
        ['Max DD', pct(r.max_drawdown), 'neg'],
        ['Trades', r.trades ?? '—', ''],
      ];
    } else if (r.type === 'optimize') {
      const hold = r.holdout || {};
      metrics = [
        ['Score', num(r.score, 3), tone(r.score)],
        ['Holdout', pct(hold.total_return), tone(hold.total_return)],
        ['Holdout SR', num(hold.sharpe), ''],
        ['Trials', r.trials ?? '—', ''],
      ];
    } else {
      metrics = [
        ['Rows', r.rows ?? '—', ''],
        ['Best', r.top?.[0]?.params ? JSON.stringify(r.top[0].params) : '—', ''],
      ];
    }
    const metricHtml = metrics.map(([k, v, t]) => `
      <div class="report-metric"><div class="k">${esc(k)}</div>
      <div class="v ${t || ''}">${esc(v)}</div></div>`).join('');
    const params = r.params ? `<div class="report-params mono">${esc(JSON.stringify(r.params))}</div>` : '';
    return `<article class="report">
      <div class="report-head">
        <div class="report-title">${esc(r.id)}</div>
        <span class="badge ${esc(r.type)}">${esc(r.type)}</span>
      </div>
      <div class="muted">${esc(r.strategy || '')} ${r.method ? `· ${esc(r.method)}` : ''}
        ${r.objective ? `· ${esc(r.objective)}` : ''} · ${esc(timeAgo(r.mtime))}</div>
      <div class="report-metrics">${metricHtml}</div>
      ${params}
      <div class="report-params"><a class="raw" href="${esc(raw)}" target="_blank" rel="noopener">raw JSON</a></div>
    </article>`;
  }).join('');
  $('#reports').innerHTML = `<div class="reports">${cards}</div>`;
}

function renderResearch(snap) {
  const research = snap.research || {};
  const audit = research.audit;
  const auditBox = $('#research-audit');
  if (!audit) {
    auditBox.innerHTML = emptyBox(
      'No audit found. Run <span class="mono">python3 scripts/audit_data.py</span>.'
    );
  } else {
    const report = audit.report || {};
    const bars = report.bars || {};
    const spreads = report.spreads || {};
    const borrow = report.borrow || {};
    const universe = report.universe || {};
    const warnings = report.warnings || [];
    const raw = `/api/report?path=${encodeURIComponent(audit.path)}`;
    const rows = [
      ['Bars', `${num(bars.symbols)} symbols · ${num(bars.rows)} rows`],
      ['Sessions', `${num(bars.timestamps)} (${num(bars.missing_sessions ? Object.keys(bars.missing_sessions).length : 0)} with gaps)`],
      ['Spreads', spreads.time_varying ? 'time series' : 'static per symbol'],
      ['Borrow', borrow.symbols ? `${num(borrow.symbols)} symbols` : 'not configured'],
      ['Universe', `${num(universe.listings)} listings · ${num(universe.delisted)} delisted`],
    ];
    auditBox.innerHTML = `
      <dl class="kv">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>
      ${warnings.length ? `<h3 class="mt">Warnings</h3>${warnings.map((w) => `<div class="alert">${esc(w)}</div>`).join('')}` : '<div class="alert ok">No audit warnings.</div>'}
      <div class="report-params"><a class="raw" href="${esc(raw)}" target="_blank" rel="noopener">raw JSON</a></div>`;
  }

  const stressBox = $('#research-stress');
  const runs = (research.stress || []).slice(0, 2);
  if (!runs.length) {
    stressBox.innerHTML = emptyBox(
      'No stress results found. Run <span class="mono">python3 scripts/stress.py configs/etf-pairs.toml configs/pca-residual.toml</span>.'
    );
    return;
  }
  stressBox.innerHTML = runs.map((run) => {
    const raw = `/api/report?path=${encodeURIComponent(run.path)}`;
    const byStrategy = {};
    for (const row of run.rows || []) {
      const key = row.strategy || 'strategy';
      byStrategy[key] ??= {};
      byStrategy[key][row.scenario] ??= {};
      byStrategy[key][row.scenario][row.period] = row;
    }
    const tables = Object.entries(byStrategy).map(([strategy, scenarios]) => {
      const rows = Object.entries(scenarios).map(([scenario, periods]) => {
        const full = periods.full || {};
        const oos = periods.oos || {};
        return [
          esc(scenario),
          pct(full.return),
          num(full.sharpe),
          pct(oos.return),
          num(oos.sharpe),
          full.rejections ?? 0,
        ];
      });
      return `<h3 class="mt">${esc(strategy)}</h3>${table(
        ['Scenario', 'Full', 'Full SR', 'OOS', 'OOS SR', 'Rej'],
        rows,
        'No rows.',
        ['', 'num', 'num', 'num', 'num', 'num'],
      )}`;
    }).join('');
    return `<div class="stress-run">
      <div class="muted">${esc(run.id)} · ${esc(timeAgo(run.mtime))} · ${(run.rows || []).length} runs</div>
      ${tables}
      <div class="report-params"><a class="raw" href="${esc(raw)}" target="_blank" rel="noopener">raw JSON</a></div>
    </div>`;
  }).join('');

}

function renderRisk(snap) {
  const risk = snap.config.risk || {};
  const rows = [
    ['Max order notional', money(risk.max_order_notional, 0)],
    ['Max position', pct(risk.max_position_pct, 0)],
    ['Max gross exposure', pct(risk.max_gross_exposure_pct, 0)],
    ['Daily loss halt', pct(risk.max_daily_loss_pct, 0)],
    ['Max open orders', risk.max_open_orders],
    ['Max data age', `${num(risk.max_data_age_s, 0)} s`],
    ['Price collar', pct(risk.price_collar_pct, 0)],
    ['Duplicate window', `${num(risk.duplicate_window_s, 0)} s`],
  ];
  $('#risk').innerHTML = `<dl class="kv">${rows.map(([k, v]) =>
    `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>`;
}

function renderExecution(snap) {
  const ex = snap.config.execution || {};
  const rows = [
    ['Slippage', `${num(ex.slippage_bps)} bps`],
    ['Half spread', `${num(ex.half_spread_bps)} bps`],
    ['Spread table', ex.spreads_csv || 'not configured'],
    ['Spread multiplier', `${num(ex.spread_multiplier)}x`],
    ['Borrow rate', `${num(ex.borrow_bps_per_year)} bps/yr`],
    ['Execution delay', `${num(ex.delay_bars)} bar(s)`],
    ['Commission / share', money(ex.commission_per_share, 4)],
    ['Minimum commission', money(ex.min_commission, 2)],
    ['Max participation', ex.max_participation ? pct(ex.max_participation, 0) : 'disabled'],
  ];
  $('#execution').innerHTML = `<dl class="kv">${rows.map(([k, v]) =>
    `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>`;
}

function renderStrategies(snap) {
  const strategies = snap.strategies || [];
  if (!strategies.length) {
    $('#strategies').innerHTML = emptyBox('No strategies registered.');
    return;
  }
  $('#strategies').innerHTML = strategies.map((s) => {
    const params = Object.entries(s.defaults || {}).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join('  ');
    return `<article class="strategy">
      <div class="strategy-head"><strong>${esc(s.id)}</strong>
        <span class="muted mono">${esc(Object.keys(s.defaults || {}).length)} params</span></div>
      <p>${esc(s.description || '')}</p>
      <div class="defaults">${esc(params || 'no parameters')}</div>
    </article>`;
  }).join('');
}

function renderHealth(snap) {
  const health = snap.health || {};
  const items = [
    ['Heartbeat', health.heartbeat_age_s == null ? 'no session' : timeAgo(snap.heartbeat?.ts)],
    ['Data feed', snap.heartbeat_stale ? 'STALE' : snap.heartbeat ? 'fresh' : 'not running'],
    ['Audit DB', health.db_state === 'ok' ? 'readable' : (health.db_error || health.db_state || 'unknown')],
    ['Disk free', health.disk_free_gb == null ? '—' : `${num(health.disk_free_gb, 1)} GB`],
    ['SoC temp', health.temperature_c == null ? '—' : `${num(health.temperature_c, 1)} °C`],
    ['Kill switch', snap.kill.engaged ? 'ENGAGED' : 'clear'],
  ];
  $('#health').innerHTML = items.map(([k, v]) => `
    <div class="health-item"><div class="label">${esc(k)}</div>
    <div class="value">${esc(v)}</div></div>`).join('');
}

function renderWarnings(snap) {
  const warnings = snap.warnings || [];
  const alert = $('#alert');
  let message = '';
  let kind = '';
  if (snap.kill.engaged) {
    message = `Kill switch engaged: ${snap.kill.reason || 'no reason recorded'}`;
    kind = 'bad';
  } else if (snap.heartbeat?.halted) {
    message = `Risk halt: ${snap.heartbeat.halt_reason || 'trading halted'}`;
    kind = 'bad';
  } else if (snap.heartbeat_stale) {
    message = `Trading heartbeat is stale (${Math.round(snap.heartbeat_age_s || 0)}s old).`;
    kind = '';
  } else if (snap.heartbeat?.stopped) {
    message = 'Trading runtime is stopped.';
    kind = '';
  }
  if (message) {
    alert.textContent = message;
    alert.className = `alert ${kind}`;
  } else {
    alert.className = 'alert hidden';
  }

  const target = $('#warnings');
  if (!warnings.length) {
    target.innerHTML = '';
    return;
  }
  target.innerHTML = `<article class="card"><div class="card-head"><h2>Warnings</h2></div>
    <div class="warn-list">${warnings.map((w) =>
      `<div class="warn-item">${esc(w)}</div>`).join('')}</div></article>`;
}

function table(headers, rows, emptyText, aligns = []) {
  if (!rows.length) return emptyBox(emptyText);
  const head = headers.map((h, i) =>
    `<th class="${aligns[i] === 'num' ? 'num' : ''}">${esc(h)}</th>`).join('');
  const body = rows.map((row) => `<tr>${row.map((cell, i) =>
    `<td class="${aligns[i] === 'num' ? 'num' : ''}">${cell}</td>`).join('')}</tr>`).join('');
  return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead>
    <tbody>${body}</tbody></table></div>`;
}

function emptyBox(text) {
  return `<div class="empty">${text}</div>`;
}

// ---------------------------------------------------------------------------
// Chart
// ---------------------------------------------------------------------------
function renderChart(snap) {
  const points = snap.equity?.points || [];
  const initial = snap.equity?.initial_cash ?? snap.account?.start_equity;
  $('#chart-run').textContent = snap.equity?.run_id || '';
  drawChart(points, initial);

  if (points.length > 1) {
    const values = points.map((p) => p.equity);
    const hi = Math.max(...values);
    const lo = Math.min(...values);
    const last = values[values.length - 1];
    $('#chart-legend').innerHTML = `
      <span><span class="swatch"></span>equity</span>
      <span>start ${esc(money(initial))}</span>
      <span>last ${esc(money(last))}</span>
      <span>high ${esc(money(hi))}</span>
      <span>low ${esc(money(lo))}</span>
      <span>${points.length} marks</span>`;
  } else {
    $('#chart-legend').innerHTML = '';
  }
}

function drawChart(points, initial) {
  const canvas = $('#chart');
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  canvas.width = Math.round(rect.width * dpr);
  canvas.height = Math.round(rect.height * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const w = rect.width;
  const h = rect.height;
  ctx.clearRect(0, 0, w, h);

  if (!points || points.length < 2) {
    ctx.fillStyle = '#5d7089';
    ctx.font = '13px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('No equity history yet', w / 2, h / 2);
    return;
  }

  const values = points.map((p) => Number(p.equity)).filter(Number.isFinite);
  const start = Number.isFinite(Number(initial)) ? Number(initial) : values[0];
  let min = Math.min(...values, start);
  let max = Math.max(...values, start);
  const pad = (max - min) * 0.12 || Math.max(1, Math.abs(max) * 0.001);
  min -= pad;
  max += pad;

  const left = 54;
  const right = 10;
  const top = 12;
  const bottom = 26;
  const plotW = w - left - right;
  const plotH = h - top - bottom;
  const x = (i) => left + (i / (points.length - 1)) * plotW;
  const y = (value) => top + (1 - (value - min) / (max - min)) * plotH;

  const accent = getComputedStyle(document.documentElement)
    .getPropertyValue('--accent').trim() || '#4fd1c5';
  const line = getComputedStyle(document.documentElement)
    .getPropertyValue('--line').trim() || '#203044';
  const muted = getComputedStyle(document.documentElement)
    .getPropertyValue('--faint').trim() || '#5d7089';

  ctx.font = '10px ui-monospace, SFMono-Regular, Menlo, monospace';
  ctx.fillStyle = muted;
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (let i = 0; i <= 4; i += 1) {
    const value = min + ((max - min) * i) / 4;
    const yy = y(value);
    ctx.strokeStyle = line;
    ctx.globalAlpha = 0.55;
    ctx.beginPath();
    ctx.moveTo(left, yy);
    ctx.lineTo(w - right, yy);
    ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.fillText(compact(value), left - 8, yy);
  }

  if (start >= min && start <= max) {
    const sy = y(start);
    ctx.strokeStyle = muted;
    ctx.globalAlpha = 0.6;
    ctx.setLineDash([3, 4]);
    ctx.beginPath();
    ctx.moveTo(left, sy);
    ctx.lineTo(w - right, sy);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;
  }

  const gradient = ctx.createLinearGradient(0, top, 0, top + plotH);
  gradient.addColorStop(0, 'rgba(79, 209, 197, 0.24)');
  gradient.addColorStop(1, 'rgba(79, 209, 197, 0.01)');
  ctx.beginPath();
  ctx.moveTo(x(0), y(values[0]));
  for (let i = 1; i < values.length; i += 1) ctx.lineTo(x(i), y(values[i]));
  ctx.lineTo(x(values.length - 1), top + plotH);
  ctx.lineTo(x(0), top + plotH);
  ctx.closePath();
  ctx.fillStyle = gradient;
  ctx.fill();

  ctx.beginPath();
  ctx.moveTo(x(0), y(values[0]));
  for (let i = 1; i < values.length; i += 1) ctx.lineTo(x(i), y(values[i]));
  ctx.strokeStyle = accent;
  ctx.lineWidth = 1.8;
  ctx.lineJoin = 'round';
  ctx.stroke();

  const lastX = x(values.length - 1);
  const lastY = y(values[values.length - 1]);
  ctx.beginPath();
  ctx.arc(lastX, lastY, 3.2, 0, Math.PI * 2);
  ctx.fillStyle = accent;
  ctx.fill();

  ctx.fillStyle = muted;
  ctx.textAlign = 'left';
  ctx.textBaseline = 'bottom';
  ctx.fillText(shortDate(points[0].ts), left, h - 6);
  ctx.textAlign = 'center';
  ctx.fillText(shortDate(points[Math.floor(points.length / 2)].ts), left + plotW / 2, h - 6);
  ctx.textAlign = 'right';
  ctx.fillText(shortDate(points[points.length - 1].ts), w - right, h - 6);
}

function compact(value) {
  const abs = Math.abs(value);
  if (abs >= 1e9) return `${(value / 1e9).toFixed(1)}B`;
  if (abs >= 1e6) return `${(value / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${(value / 1e3).toFixed(1)}k`;
  return value.toFixed(0);
}

// ---------------------------------------------------------------------------
// UI plumbing
// ---------------------------------------------------------------------------
function switchTab(name) {
  state.tab = name;
  $$('.tab').forEach((tab) => tab.classList.toggle('active', tab.dataset.tab === name));
  $$('.panel').forEach((panel) => panel.classList.toggle('active', panel.id === `panel-${name}`));
  if (name === 'overview' && state.snap) {
    drawChart(state.snap.equity?.points || [], state.snap.equity?.initial_cash);
  }
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function toast(message, kind = '') {
  const el = $('#toast');
  if (!el) return;
  el.textContent = message;
  el.className = `toast show ${kind}`;
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => { el.className = 'toast'; }, 3000);
}

async function logout() {
  try {
    await fetch('/logout', { method: 'POST', credentials: 'same-origin' });
  } finally {
    window.location.assign('/login');
  }
}

function bind() {
  $$('.tab').forEach((tab) => {
    tab.addEventListener('click', () => switchTab(tab.dataset.tab));
  });
  $$('[data-goto]').forEach((button) => {
    button.addEventListener('click', () => switchTab(button.dataset.goto));
  });
  $('#refresh')?.addEventListener('click', () => load());
  $('#lock')?.addEventListener('click', logout);
  window.addEventListener('resize', () => {
    window.clearTimeout(bind.resizeTimer);
    bind.resizeTimer = window.setTimeout(() => {
      if (state.snap) drawChart(state.snap.equity?.points || [], state.snap.equity?.initial_cash);
    }, 120);
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) load();
  });
  window.addEventListener('online', () => load());
}

function start() {
  const params = new URLSearchParams(window.location.search);
  const requested = Number(params.get('refresh'));
  if (Number.isFinite(requested) && requested >= 1) {
    state.refreshMs = Math.min(60, Math.max(1, requested)) * 1000;
  }
  bind();
  load();
  state.timer = window.setInterval(() => {
    if (!document.hidden) load();
  }, state.refreshMs);
}

start();
