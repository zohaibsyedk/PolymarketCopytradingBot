/* PolyCopy web terminal - vanilla JS, no build step. */
"use strict";

// ------------------------------------------------------------------ helpers
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
};

function usd(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  const abs = Math.abs(v);
  const s = abs >= 1e6 ? (abs / 1e6).toFixed(2) + "M" : abs.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return (v < 0 ? "−$" : "$") + s;
}
function signedUsd(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  if (Math.abs(v) < 0.005) return "$0.00";
  return (v > 0 ? "+" : "") + usd(v);
}
function pct(v, digits = 1) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  return (v * 100).toFixed(digits) + "%";
}
function signedPct(v, digits = 1) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  return (v > 0 ? "+" : "") + (v * 100).toFixed(digits) + "%";
}
function cents(p) {
  if (p === null || p === undefined || Number.isNaN(p)) return "–";
  return (p * 100).toFixed(1) + "¢";
}
function num(v, digits = 1) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  return Number(v).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}
function deltaClass(v) { return v > 0.004 ? "delta up" : v < -0.004 ? "delta down" : "delta"; }
function timeOf(ts) {
  if (!ts) return "–";
  const d = new Date(ts * 1000);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return sameDay ? t : d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + t.slice(0, 5);
}
function ago(ts) {
  if (!ts) return "–";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return Math.round(s) + "s ago";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
}
function duration(sec) {
  if (sec === null || sec === undefined) return "–";
  sec = Math.abs(sec);
  if (sec < 3600) return Math.round(sec / 60) + "m";
  if (sec < 86400) return Math.floor(sec / 3600) + "h " + Math.round((sec % 3600) / 60) + "m";
  return Math.floor(sec / 86400) + "d " + Math.round((sec % 86400) / 3600) + "h";
}
function countdown(ts) {
  if (!ts) return "–";
  const s = ts - Date.now() / 1000;
  return s <= 0 ? "awaiting result" : "in " + duration(s);
}
function shortAddr(a) { return a ? a.slice(0, 6) + "…" + a.slice(-4) : "–"; }
function marketLink(row) {
  const slug = row.event_slug || row.slug;
  return slug ? `https://polymarket.com/event/${encodeURIComponent(slug)}` : null;
}
function tag(text, kind) { return `<span class="tag ${kind || ""}">${esc(text)}</span>`; }

let toastTimer = null;
function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = "toast" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), isError ? 7000 : 3500);
}

async function api(path, options = {}) {
  const init = { method: options.method || "GET", headers: {} };
  if (options.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, init);
  let data = null;
  try { data = await response.json(); } catch { /* empty body */ }
  if (!response.ok) {
    const detail = data && data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : response.statusText;
    throw new Error(detail);
  }
  return data;
}

// ------------------------------------------------------------------ state
const app = {
  state: null,
  tab: "dashboard",
  positionsStatus: "open",
  signalFilter: "",
  traderFilter: "",
  logFilter: "",
  equityDays: 30,
  charts: {},
  settings: null,
  presets: null,
  defaults: null,
  wizardLoggedIn: false,
};

// ------------------------------------------------------------------ theme
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  store.set("theme", theme);
  Object.values(app.charts).forEach((c) => c && c.destroy());
  app.charts = {};
  refreshActive(true);
}
function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

// ------------------------------------------------------------------ header & banners
function renderHeader() {
  const s = app.state;
  if (!s) return;
  const status = $("#status-pill");
  status.textContent = { running: "Running", paused: "Paused", stopped: "Stopped" }[s.status] || s.status;
  status.className = "pill " + s.status;
  const mode = $("#mode-pill");
  mode.textContent = s.mode === "live" ? "LIVE" : "Paper";
  mode.className = "pill pill-mode" + (s.mode === "live" ? " live" : "");
  $("#demo-badge").classList.toggle("hidden", !s.demo);
  const stream = $("#stream-pill");
  if (s.status === "stopped") {
    stream.textContent = "Feed: off";
    stream.className = "pill pill-quiet";
  } else if (s.stream && s.stream.connected) {
    stream.textContent = "Feed: realtime";
    stream.className = "pill pill-quiet on";
  } else {
    stream.textContent = "Feed: polling";
    stream.className = "pill pill-quiet poll";
  }
  stream.title = `Leaders watched: ${s.watcher.leaders} · polls: ${s.watcher.polls} · signals: ${s.watcher.signals_seen}`;
  $("#btn-start").textContent = s.status === "paused" ? "Resume" : "Start";
  $("#btn-start").disabled = s.status === "running";
  $("#btn-pause").disabled = s.status !== "running";
  $("#btn-stop").disabled = s.status === "stopped";
  const chip = $("#wallet-chip");
  if (s.account) {
    chip.textContent = `${shortAddr(s.account.wallet)} · ${usd(s.account.cash)}`;
    chip.title = `${s.account.wallet_type} wallet ${s.account.wallet}`;
  } else {
    chip.textContent = "Connect wallet";
    chip.title = "";
  }
  $("#nav-foot").innerHTML = `v${esc(s.version)}<br>${s.followed.length} leaders followed`;
  renderBanners();
}

function renderBanners() {
  const s = app.state;
  const items = [];
  if (s.demo) items.push(["info", "Demo mode: this is a simulated market with fake traders. Nothing here is real money or real Polymarket data."]);
  if (s.login_error && !s.logged_in) items.push(["bad", `Automatic login failed: ${s.login_error}. Open "Connect wallet" to log in again.`]);
  if (!s.logged_in && s.mode === "paper" && !s.demo) items.push(["info", "Paper trading without a wallet. Connect your Polymarket wallet when you are ready to trade live."]);
  if (s.geo && s.geo.blocked) items.push(["bad", `Polymarket reports your location (${s.geo.country || "unknown"}${s.geo.region ? " / " + s.geo.region : ""}) as restricted. Live orders will be rejected; PolyCopy will not place them.`]);
  if (s.account && s.account.closed_only) items.push(["bad", "Your Polymarket account is in close-only mode: new positions cannot be opened."]);
  if (s.status === "paused" && s.pause_reason && s.pause_reason !== "paused by user") items.push(["warn", `New entries paused: ${s.pause_reason}`]);
  if (s.mode === "live" && s.account && !s.account.has_relayer_key) items.push(["warn", "PolyCopy could not set up gasless redemption for this wallet. Winning positions may need to be claimed on polymarket.com."]);
  if (s.status !== "stopped" && s.stream && !s.stream.connected && !s.demo) items.push(["warn", "Realtime trade feed is disconnected; falling back to polling every few seconds (slower copies)."]);
  if (s.discovery && s.discovery.error) items.push(["warn", `Last trader scan failed: ${s.discovery.error}`]);
  if (s.status !== "stopped" && s.followed.length === 0 && !(s.discovery && s.discovery.running)) items.push(["warn", "No leaders are being followed yet. Run a scan from the Traders tab or follow a wallet manually."]);
  $("#banners").innerHTML = items.map(([k, t]) => `<div class="banner ${k}">${esc(t)}</div>`).join("");
}

// ------------------------------------------------------------------ dashboard
function renderTiles() {
  const s = app.state;
  if (!s) return;
  const m = s.summary;
  $("#hero-mode").textContent = s.mode === "live" ? "· live wallet" : "· paper account";
  $("#hero-equity").textContent = usd(m.equity);
  const pnl = $("#hero-pnl");
  pnl.textContent = signedUsd(m.total_pnl) + (m.base_capital ? ` (${signedPct(m.total_pnl / m.base_capital)})` : "");
  pnl.className = deltaClass(m.total_pnl);
  const day = $("#hero-day");
  day.textContent = signedUsd(m.day_pnl);
  day.className = deltaClass(m.day_pnl);
  const tiles = [
    ["Cash", usd(m.cash), m.unredeemed ? `${usd(m.unredeemed)} to claim` : "available to trade"],
    ["Invested", usd(m.exposure), `${m.open_positions} open position${m.open_positions === 1 ? "" : "s"}`],
    ["Open value", usd(m.positions_value), `unrealized ${signedUsd(m.unrealized_pnl)}`],
    ["Realized P&L", signedUsd(m.realized_pnl), `${m.closed_positions} closed`, m.realized_pnl],
    ["Win rate", m.win_rate === null ? "–" : pct(m.win_rate, 0), "of closed positions"],
    ["Fees paid", usd(m.fees_paid), "Polymarket taker fees"],
    ["Leaders", String(s.followed.length), s.discovery.finished_at ? `scanned ${ago(s.discovery.finished_at)}` : "not scanned yet"],
    ["Signals", String(s.watcher.signals_seen), s.started_at ? `since start ${ago(s.started_at)}` : "bot stopped"],
  ];
  $("#tiles").innerHTML = tiles.map(([label, value, sub, sign]) => `
    <div class="tile"><div class="label">${esc(label)}</div>
      <div class="value ${sign !== undefined ? deltaClass(sign) : ""}">${esc(value)}</div>
      <div class="sub">${esc(sub)}</div></div>`).join("");
  renderDiscovery();
}

function renderDiscovery() {
  const d = app.state && app.state.discovery;
  const card = $("#discovery-card");
  if (!d || !d.running) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  const phase = { leaderboards: "reading leaderboards…", scoring: "replaying each trader's last month of trades…", selecting: "choosing leaders…" }[d.phase] || d.phase;
  $("#discovery-phase").textContent = phase;
  $("#discovery-count").textContent = d.total ? `${d.done} / ${d.total} wallets` : "";
  $("#discovery-fill").style.width = d.total ? `${Math.round((100 * d.done) / d.total)}%` : "8%";
}

async function loadDashboard() {
  renderTiles();
  const [positions, signals, equity, analytics] = await Promise.all([
    api("/api/positions?status=open"),
    api("/api/signals?limit=40"),
    api(`/api/equity?days=${app.equityDays}`),
    api("/api/analytics"),
  ]);
  renderDashPositions(positions.slice(0, 8));
  renderFeed(signals);
  renderLeaderStrip();
  drawEquity(equity);
  drawDaily(analytics.daily_pnl);
}

function renderDashPositions(rows) {
  const el = $("#dash-positions");
  if (!rows.length) { el.innerHTML = `<tr class="empty"><td>No open positions. New copies appear here.</td></tr>`; return; }
  el.innerHTML = `<thead><tr><th>Market</th><th class="num">Value</th><th class="num">P&amp;L</th><th class="num">Resolves</th></tr></thead><tbody>` +
    rows.map((r) => `<tr><td class="title-cell"><div>${esc(r.title)}</div><span class="muted small">${esc(r.outcome || "")} · ${num(r.shares, 1)} sh @ ${cents(r.avg_price)}</span></td>
      <td class="num">${usd(r.value)}</td><td class="num ${deltaClass(r.unrealized_pnl)}">${signedUsd(r.unrealized_pnl)}</td>
      <td class="num muted">${countdown(r.end_ts)}</td></tr>`).join("") + "</tbody>";
}

function feedItem(s) {
  const verb = s.side === "BUY" ? "bought" : s.kind === "MERGE" ? "merged" : "sold";
  const who = esc(s.leader_name || shortAddr(s.wallet));
  let outcome;
  if (s.decision === "copied") outcome = `${tag("Copied", "good")} ${s.stake ? usd(s.stake) : ""}`;
  else if (s.decision === "exited") outcome = tag("Exited", "info");
  else if (s.decision === "pending") outcome = tag("Exit pending", "warn");
  else if (s.decision === "failed") outcome = tag("Failed", "bad");
  else outcome = tag("Skipped", "");
  return `<li><span class="time">${timeOf(s.detected_at).slice(0, 8)}</span><div>
    <div><strong>${who}</strong> ${verb} ${esc(s.outcome || "")} @ ${cents(s.leader_price)} <span class="muted">(${usd(s.leader_usdc, 0)})</span> ${outcome}</div>
    <div class="why">${esc(s.title || "")}${s.reason ? " — " + esc(s.reason) : ""}</div></div></li>`;
}

function renderFeed(rows) {
  const el = $("#dash-feed");
  el.innerHTML = rows.length ? rows.map(feedItem).join("") : `<li><span></span><span class="muted">Waiting for followed leaders to trade…</span></li>`;
}

function renderLeaderStrip() {
  const leaders = (app.state && app.state.followed) || [];
  const el = $("#dash-leaders");
  if (!leaders.length) { el.innerHTML = `<span class="muted">No leaders yet — the first scan starts when the bot starts.</span>`; return; }
  el.innerHTML = leaders.map((l) => `<div class="leader-chip" data-wallet="${esc(l.wallet)}">
      <strong>${esc(l.name || shortAddr(l.wallet))}</strong>
      <span class="muted small">score ${num(l.score, 0)} · ${shortAddr(l.wallet)}</span></div>`).join("");
  $$(".leader-chip", el).forEach((c) => c.addEventListener("click", () => openTrader(c.dataset.wallet)));
}

// ------------------------------------------------------------------ charts
function baseOptions() {
  const grid = css("--grid"), muted = css("--text-muted"), surface = css("--surface-1"), text = css("--text-primary");
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: surface, titleColor: text, bodyColor: text, borderColor: css("--axis"), borderWidth: 1,
        padding: 10, displayColors: true, boxWidth: 8, boxHeight: 8, usePointStyle: true,
      },
    },
    scales: {
      x: { grid: { color: grid, drawTicks: false }, border: { color: css("--axis") }, ticks: { color: muted, maxRotation: 0, autoSkipPadding: 18, padding: 6 } },
      y: { grid: { color: grid, drawTicks: false }, border: { display: false }, ticks: { color: muted, padding: 6 } },
    },
  };
}

function upsertChart(key, canvas, config) {
  const existing = app.charts[key];
  if (existing && existing.config.type === config.type) {
    existing.data = config.data;
    existing.options = config.options;
    existing.update("none");
    return existing;
  }
  if (existing) existing.destroy();
  app.charts[key] = new Chart(canvas, config);
  return app.charts[key];
}

function drawEquity(rows) {
  const empty = rows.length < 2;
  $("#equity-empty").classList.toggle("hidden", !empty);
  const color = css("--series-1");
  const opts = baseOptions();
  opts.scales.x.type = "linear";
  const span = rows.length > 1 ? rows[rows.length - 1].ts - rows[0].ts : 0;
  opts.scales.x.ticks.callback = (v) => {
    const d = new Date(v);
    return span < 2 * 86400 ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : d.toLocaleDateString([], { month: "short", day: "numeric" });
  };
  opts.scales.y.ticks.callback = (v) => usd(v, 0);
  opts.plugins.tooltip.callbacks = {
    title: (items) => new Date(items[0].parsed.x).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }),
    label: (item) => ` Equity ${usd(item.parsed.y)}`,
  };
  upsertChart("equity", $("#chart-equity"), {
    type: "line",
    data: { datasets: [{
      label: "Equity", data: rows.map((r) => ({ x: r.ts * 1000, y: r.equity })),
      borderColor: color, backgroundColor: hexAlpha(color, 0.10), fill: "origin", borderWidth: 2,
      pointRadius: 0, pointHoverRadius: 5, pointHoverBorderWidth: 2, pointHoverBorderColor: css("--surface-1"),
      tension: 0.15, borderJoinStyle: "round", borderCapStyle: "round",
    }] },
    options: Object.assign(opts, { scales: Object.assign(opts.scales, { y: Object.assign(opts.scales.y, { grace: "5%" }) }) }),
  });
}

function hexAlpha(hex, alpha) {
  const h = hex.replace("#", "");
  const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

function polarityBars(key, canvas, labels, values, { horizontal = false, fmt = usd, tooltipExtra = null } = {}) {
  const pos = css("--pos"), neg = css("--neg");
  const opts = baseOptions();
  opts.interaction = { mode: "nearest", intersect: false, axis: horizontal ? "y" : "x" };
  if (horizontal) {
    opts.indexAxis = "y";
    opts.scales.x.ticks.callback = (v) => fmt(v);
    opts.scales.x.beginAtZero = true;
    opts.scales.y.grid.display = false;
  } else {
    opts.scales.y.ticks.callback = (v) => fmt(v);
    opts.scales.y.beginAtZero = true;
    opts.scales.x.grid.display = false;
  }
  opts.plugins.tooltip.callbacks = {
    label: (item) => ` ${fmt(item.raw)}${tooltipExtra ? " · " + tooltipExtra(item.dataIndex) : ""}`,
  };
  return upsertChart(key, canvas, {
    type: "bar",
    data: { labels, datasets: [{
      data: values, backgroundColor: values.map((v) => (v >= 0 ? pos : neg)),
      borderRadius: 4, borderSkipped: "start", maxBarThickness: 24, categoryPercentage: 0.8, barPercentage: 0.9,
    }] },
    options: opts,
  });
}

function drawDaily(rows) {
  const recent = rows.slice(-30);
  $("#daily-empty").classList.toggle("hidden", recent.length > 0);
  polarityBars("daily", $("#chart-daily"), recent.map((r) => r.day.slice(5)), recent.map((r) => r.pnl));
}

// ------------------------------------------------------------------ positions
async function loadPositions() {
  const rows = await api(`/api/positions?status=${app.positionsStatus}`);
  const el = $("#positions-table");
  $("#btn-close-all").classList.toggle("hidden", app.positionsStatus !== "open" || !rows.length);
  if (!rows.length) {
    el.innerHTML = `<tr class="empty"><td>${app.positionsStatus === "open" ? "No open positions." : "No closed positions yet."}</td></tr>`;
    return;
  }
  if (app.positionsStatus === "open") {
    el.innerHTML = `<thead><tr><th>Market</th><th>Copied from</th><th class="num">Shares</th><th class="num">Entry → now</th><th class="num">Cost</th><th class="num">Value</th><th class="num">Unrealized</th><th class="num">Resolves</th><th></th></tr></thead><tbody>` +
      rows.map((r) => {
        const link = marketLink(r);
        const pnlPct = r.cost ? r.unrealized_pnl / r.cost : null;
        return `<tr><td class="title-cell"><div>${link ? `<a href="${esc(link)}" target="_blank" rel="noopener">${esc(r.title)}</a>` : esc(r.title)}</div>
          ${tag(r.outcome || "?", "info")} ${r.exit_pending ? tag("exit pending", "warn") : ""} <span class="muted small">opened ${ago(r.opened_at)}</span></td>
          <td class="note-cell" title="${esc((r.leader_names || []).join(", "))}">${esc((r.leader_names || []).join(", "))}</td>
          <td class="num">${num(r.shares, 2)}</td><td class="num">${cents(r.avg_price)} → ${cents(r.last_price)}</td>
          <td class="num">${usd(r.cost)}</td><td class="num">${usd(r.value)}</td>
          <td class="num ${deltaClass(r.unrealized_pnl)}">${signedUsd(r.unrealized_pnl)}<br><span class="small">${signedPct(pnlPct)}</span></td>
          <td class="num">${countdown(r.end_ts)}</td>
          <td><button class="btn btn-xs" data-close="${r.id}">Close</button></td></tr>`;
      }).join("") + "</tbody>";
    $$("[data-close]", el).forEach((b) => b.addEventListener("click", () => closePosition(b.dataset.close)));
  } else {
    el.innerHTML = `<thead><tr><th>Market</th><th>Result</th><th>Copied from</th><th class="num">Entry</th><th class="num">Invested</th><th class="num">Realized</th><th class="num">ROI</th><th class="num">Held</th><th class="num">Closed</th></tr></thead><tbody>` +
      rows.map((r) => {
        const kind = r.result === "won" ? "good" : r.result === "lost" ? "bad" : "";
        const label = { won: "Won", lost: "Lost", sold: "Sold", split: "50/50" }[r.result] || r.result || r.status;
        const link = marketLink(r);
        return `<tr><td class="title-cell"><div>${link ? `<a href="${esc(link)}" target="_blank" rel="noopener">${esc(r.title)}</a>` : esc(r.title)}</div><span class="muted small">${esc(r.outcome || "")}</span></td>
          <td>${tag(label, kind)}${r.status === "resolved" && r.mode === "live" && !r.redeemed && r.payout > 0 ? " " + tag("to claim", "warn") : ""}</td>
          <td>${esc((r.leader_names || []).join(", "))}</td>
          <td class="num">${cents(r.entry_price)}</td><td class="num">${usd(r.invested)}</td>
          <td class="num ${deltaClass(r.realized_pnl)}">${signedUsd(r.realized_pnl)}</td>
          <td class="num ${deltaClass(r.realized_pnl)}">${r.invested ? signedPct(r.realized_pnl / r.invested) : "–"}</td>
          <td class="num">${duration((r.closed_at || 0) - (r.opened_at || 0))}</td><td class="num muted">${timeOf(r.closed_at)}</td></tr>`;
      }).join("") + "</tbody>";
  }
}

async function closePosition(id) {
  if (!confirm("Sell this whole position at the current best bid?")) return;
  try {
    const r = await api(`/api/positions/${id}/close`, { method: "POST" });
    toast(r.result === "sold" ? "Position sold" : "No buyers near the price yet; PolyCopy will keep trying");
    loadPositions();
  } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------------ orders
async function loadOrders() {
  const rows = await api("/api/orders?limit=500");
  const el = $("#orders-table");
  if (!rows.length) { el.innerHTML = `<tr class="empty"><td>No orders yet.</td></tr>`; return; }
  const purpose = { entry: "Copy entry", mirror_exit: "Leader exit", reconcile_exit: "Leader exit (reconciled)", take_profit: "Take profit", manual_close: "Manual close" };
  el.innerHTML = `<thead><tr><th>Time</th><th>Side</th><th>Reason</th><th>Market</th><th class="num">Fill price</th><th class="num">Leader price</th><th class="num">Slippage</th><th class="num">Shares</th><th class="num">USDC</th><th class="num">Fee</th><th>Status</th></tr></thead><tbody>` +
    rows.map((r) => {
      const st = r.status === "filled" ? tag("Filled", "good") : r.status === "partial" ? tag("Partial", "warn") : tag("Failed", "bad");
      return `<tr><td class="muted">${timeOf(r.created_at)}</td><td class="${r.side === "BUY" ? "side-buy" : "side-sell"}">${r.side}</td>
        <td>${esc(purpose[r.purpose] || r.purpose)}</td>
        <td class="title-cell"><div>${esc(r.title || r.asset_id)}</div><span class="muted small">${esc(r.outcome || "")}</span></td>
        <td class="num">${cents(r.avg_price)}</td><td class="num">${cents(r.leader_price)}</td>
        <td class="num">${r.slippage === null || r.slippage === undefined ? "–" : (r.slippage * 100).toFixed(2) + "¢"}</td>
        <td class="num">${num(r.shares, 2)}</td><td class="num">${usd(r.usdc)}</td><td class="num">${usd(r.fee, 3)}</td>
        <td>${st}${r.error ? `<div class="muted small">${esc(r.error)}</div>` : ""}</td></tr>`;
    }).join("") + "</tbody>";
}

// ------------------------------------------------------------------ signals
async function loadSignals() {
  const q = app.signalFilter ? `&decision=${app.signalFilter}` : "";
  const rows = await api(`/api/signals?limit=500${q}`);
  const el = $("#signals-table");
  if (!rows.length) { el.innerHTML = `<tr class="empty"><td>No signals yet. They appear when a followed leader trades.</td></tr>`; return; }
  el.innerHTML = `<thead><tr><th>Detected</th><th>Leader</th><th>Action</th><th>Market</th><th class="num">Leader price</th><th class="num">Leader size</th><th class="num">Delay</th><th>Decision</th><th>Details</th></tr></thead><tbody>` +
    rows.map((s) => {
      const dec = { copied: tag("Copied", "good"), exited: tag("Exited", "info"), pending: tag("Exit pending", "warn"), failed: tag("Failed", "bad"), skipped: tag("Skipped", "") }[s.decision] || tag(s.decision);
      const action = s.kind === "MERGE" ? "MERGE" : s.side;
      return `<tr><td class="muted">${timeOf(s.detected_at)}</td>
        <td><a data-wallet="${esc(s.wallet)}">${esc(s.leader_name || shortAddr(s.wallet))}</a></td>
        <td class="${s.side === "BUY" ? "side-buy" : "side-sell"}">${action}</td>
        <td class="title-cell"><div>${esc(s.title || "")}</div><span class="muted small">${esc(s.outcome || "")}</span></td>
        <td class="num">${cents(s.leader_price)}</td><td class="num">${usd(s.leader_usdc, 0)}</td>
        <td class="num">${num(s.latency_sec, 1)}s <span class="muted small">${esc(s.source || "")}</span></td>
        <td>${dec}</td><td class="small reason-cell">${s.stake ? "stake " + usd(s.stake) + (s.reason ? " · " : "") : ""}${esc(s.reason || "")}</td></tr>`;
    }).join("") + "</tbody>";
  $$("a[data-wallet]", el).forEach((a) => a.addEventListener("click", () => openTrader(a.dataset.wallet)));
}

// ------------------------------------------------------------------ traders
function statusTag(status) {
  return { followed: tag("Following", "good"), candidate: tag("Candidate", ""), benched: tag("Benched", "warn"), blocked: tag("Blocked", "bad") }[status] || tag(status);
}
function scoreBar(score) {
  const s = Math.max(0, Math.min(100, score || 0));
  return `<span class="scorebar"><span class="track"><span class="fill" style="width:${s}%"></span></span>${num(score, 0)}</span>`;
}

async function loadTraders() {
  const q = app.traderFilter ? `?status=${app.traderFilter}` : "";
  const rows = await api(`/api/traders${q}`);
  const el = $("#traders-table");
  if (!rows.length) { el.innerHTML = `<tr class="empty"><td>No traders scored yet. Press "Re-scan leaderboards" or start the bot.</td></tr>`; return; }
  el.innerHTML = `<thead><tr><th>Status</th><th>Trader</th><th>Score</th><th class="num">Copy ROI</th><th class="num">Win rate</th><th class="num">Positions</th><th class="num">Skill z</th><th class="num">Profit factor</th><th class="num">Hrs to resolve</th><th class="num">Orders/day</th><th class="num">Live copies</th><th>Notes</th><th></th></tr></thead><tbody>` +
    rows.map((t) => {
      const m = t.metrics || {};
      const live = t.live || {};
      const followBtn = t.status === "followed" ? `<button class="btn btn-xs" data-act="unfollow" data-w="${esc(t.wallet)}">Unfollow</button>` : t.status === "blocked" ? `<button class="btn btn-xs" data-act="unblock" data-w="${esc(t.wallet)}">Unblock</button>` : `<button class="btn btn-xs" data-act="follow" data-w="${esc(t.wallet)}">Follow</button>`;
      const blockBtn = t.status !== "blocked" ? `<button class="btn btn-xs btn-ghost" data-act="block" data-w="${esc(t.wallet)}">Block</button>` : "";
      return `<tr class="clickable" data-open="${esc(t.wallet)}">
        <td>${statusTag(t.status)}${t.manual ? " " + tag("manual", "info") : ""}</td>
        <td><strong>${esc(t.name || shortAddr(t.wallet))}</strong><div class="muted small mono">${shortAddr(t.wallet)}</div></td>
        <td>${scoreBar(t.score)}</td>
        <td class="num ${deltaClass(m.roi)}">${m.closed_positions ? signedPct(m.roi) : "–"}</td>
        <td class="num">${m.closed_positions ? pct(m.win_rate, 0) : "–"}</td>
        <td class="num">${num(m.closed_positions, 0)}</td>
        <td class="num">${m.closed_positions ? num(m.skill_z, 2) : "–"}</td>
        <td class="num">${m.closed_positions ? num(Math.min(m.profit_factor, 99), 2) : "–"}</td>
        <td class="num">${m.avg_hours_to_resolution ? num(m.avg_hours_to_resolution, 1) : "–"}</td>
        <td class="num">${num(m.trades_per_day, 1)}</td>
        <td class="num">${live.closed ? `${live.closed} · <span class="${deltaClass(live.pnl)}">${signedUsd(live.pnl)}</span>` : "–"}</td>
        <td class="small muted note-cell" title="${esc(t.note || "")}">${esc(t.note || "")}</td>
        <td class="num">${followBtn} ${blockBtn}</td></tr>`;
    }).join("") + "</tbody>";
  $$("button[data-act]", el).forEach((b) => b.addEventListener("click", async (ev) => {
    ev.stopPropagation();
    try { await api(`/api/traders/${b.dataset.w}/${b.dataset.act}`, { method: "POST" }); toast("Updated"); loadTraders(); refreshState(); }
    catch (e) { toast(e.message, true); }
  }));
  $$("tr[data-open]", el).forEach((tr) => tr.addEventListener("click", () => openTrader(tr.dataset.open)));
}

async function openTrader(wallet) {
  const drawer = $("#drawer");
  drawer.classList.remove("hidden");
  drawer.setAttribute("aria-hidden", "false");
  $("#drawer-body").innerHTML = `<p class="muted">Loading…</p>`;
  let t;
  try { t = await api(`/api/traders/${wallet}`); } catch (e) { $("#drawer-body").innerHTML = `<p>${esc(e.message)}</p>`; return; }
  const m = t.metrics || {};
  const live = t.live || {};
  $("#drawer-title").textContent = t.name || shortAddr(t.wallet);
  const boards = Object.entries(t.leaderboard || {}).map(([k, v]) => `${esc(k)}: #${v.rank} (${usd(v.pnl, 0)})`).join(" · ");
  const kv = [
    ["Score", num(t.score, 1)], ["Copy ROI (sim)", m.closed_positions ? signedPct(m.roi) : "–"], ["Sim P&L per $100", m.closed_positions ? signedUsd(m.total_pnl / Math.max(1, m.total_invested / 100)) : "–"],
    ["Closed positions", num(m.closed_positions, 0)], ["Win rate", m.closed_positions ? `${pct(m.win_rate, 0)} (≥${pct(m.win_rate_lower, 0)})` : "–"], ["Price-implied win rate", m.resolved_positions ? pct(m.expected_win_rate, 0) : "–"],
    ["Skill z-score", num(m.skill_z, 2)], ["t-stat", num(m.t_stat, 2)], ["Profit factor", num(Math.min(m.profit_factor || 0, 99), 2)],
    ["Profitable days", m.closed_positions ? pct(m.profitable_days, 0) : "–"], ["1st / 2nd half", `${signedUsd(m.first_half_pnl)} / ${signedUsd(m.second_half_pnl)}`], ["Max drawdown (sim)", usd(m.max_drawdown)],
    ["Biggest win share", m.closed_positions ? pct(m.concentration, 0) : "–"], ["Avg entry price", cents(m.avg_entry_price)], ["Avg hours to resolve", num(m.avg_hours_to_resolution, 1)],
    ["Orders / day", num(m.trades_per_day, 1)], ["Short-dated share", pct(m.short_horizon_share, 0)], ["Median trade", usd(m.median_trade_usdc, 0)],
    ["Live copies closed", num(live.closed, 0)], ["Live P&L", signedUsd(live.pnl)], ["Live ROI", live.invested ? signedPct(live.roi) : "–"],
  ];
  const actions = [
    t.status === "followed" ? ["unfollow", "Unfollow"] : ["follow", "Follow"],
    t.status === "blocked" ? ["unblock", "Unblock"] : ["block", "Block"],
  ];
  if (t.manual) actions.push(["auto", "Let PolyCopy decide"]);
  const skips = Object.entries(m.skip_reasons || {}).sort((a, b) => b[1] - a[1]).map(([k, v]) => `${esc(k)} (${v})`).join(", ");
  $("#drawer-body").innerHTML = `
    <div class="row">${statusTag(t.status)} ${t.manual ? tag("manual", "info") : ""}
      <a href="https://polymarket.com/profile/${esc(t.wallet)}" target="_blank" rel="noopener">Polymarket profile ↗</a>
      <span class="mono muted small">${esc(t.wallet)}</span></div>
    <div class="row" style="margin-top:8px">${actions.map(([a, l]) => `<button class="btn btn-xs" data-dact="${a}">${l}</button>`).join("")}</div>
    ${t.note ? `<p class="muted small">${esc(t.note)}</p>` : ""}
    <div class="kv">${kv.map(([k, v]) => `<div><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`).join("")}</div>
    <div class="card-head"><h3>Simulated copy P&amp;L, last ${esc(app.settings ? app.settings.discovery.lookback_days : 30)} days</h3><span class="muted small">$100 per copied trade, with slippage &amp; fees</span></div>
    <div class="chart-box"><canvas id="chart-trader"></canvas></div>
    ${boards ? `<p class="muted small">Leaderboards: ${boards}</p>` : ""}
    ${skips ? `<p class="muted small">Trades the simulation would skip: ${skips}</p>` : ""}
    <div class="card-head" style="margin-top:12px"><h3>Open copies from this leader</h3></div>
    <table class="table">${(t.open_lots || []).length ? `<thead><tr><th>Market</th><th class="num">Shares</th><th class="num">Cost</th></tr></thead><tbody>` + t.open_lots.map((l) => `<tr><td class="title-cell"><div>${esc(l.title)}</div><span class="muted small">${esc(l.outcome || "")}</span></td><td class="num">${num(l.shares, 2)}</td><td class="num">${usd(l.cost)}</td></tr>`).join("") + "</tbody>" : `<tr class="empty"><td>None</td></tr>`}</table>
    <div class="card-head" style="margin-top:12px"><h3>Recent trades seen</h3></div>
    <table class="table">${(t.recent_trades || []).length ? `<thead><tr><th>Time</th><th>Side</th><th>Market</th><th class="num">Price</th><th class="num">USDC</th></tr></thead><tbody>` + t.recent_trades.slice(0, 40).map((r) => `<tr><td class="muted">${timeOf(r.ts)}</td><td class="${r.side === "BUY" ? "side-buy" : "side-sell"}">${esc(r.kind === "MERGE" ? "MERGE" : r.side)}</td><td class="title-cell"><div>${esc(r.title || "")}</div><span class="muted small">${esc(r.outcome || "")}</span></td><td class="num">${cents(r.price)}</td><td class="num">${usd(r.usdc, 0)}</td></tr>`).join("") + "</tbody>" : `<tr class="empty"><td>No trades seen since following.</td></tr>`}</table>`;
  $$("[data-dact]").forEach((b) => b.addEventListener("click", async () => {
    try { await api(`/api/traders/${t.wallet}/${b.dataset.dact}`, { method: "POST" }); toast("Updated"); openTrader(t.wallet); refreshState(); if (app.tab === "traders") loadTraders(); }
    catch (e) { toast(e.message, true); }
  }));
  const curve = (t.curve || []).map(([ts, v]) => ({ x: ts * 1000, y: v }));
  const opts = baseOptions();
  opts.scales.x.type = "linear";
  opts.scales.x.ticks.callback = (v) => new Date(v).toLocaleDateString([], { month: "short", day: "numeric" });
  opts.scales.y.ticks.callback = (v) => usd(v, 0);
  opts.plugins.tooltip.callbacks = { title: (i) => new Date(i[0].parsed.x).toLocaleDateString(), label: (i) => ` Cumulative ${signedUsd(i.parsed.y)}` };
  if (app.charts.trader) { app.charts.trader.destroy(); delete app.charts.trader; }
  const color = css("--series-1");
  app.charts.trader = new Chart($("#chart-trader"), {
    type: "line",
    data: { datasets: [{ data: curve, borderColor: color, backgroundColor: hexAlpha(color, 0.1), fill: "origin", borderWidth: 2, pointRadius: 0, pointHoverRadius: 5, tension: 0.1 }] },
    options: opts,
  });
}

function closeDrawer() {
  $("#drawer").classList.add("hidden");
  $("#drawer").setAttribute("aria-hidden", "true");
  if (app.charts.trader) { app.charts.trader.destroy(); delete app.charts.trader; }
}

// ------------------------------------------------------------------ analytics
async function loadAnalytics() {
  const a = await api("/api/analytics");
  const m = app.state ? app.state.summary : {};
  const decisions = Object.fromEntries((a.decisions || []).map((d) => [d.decision, d.n]));
  const totalSignals = Object.values(decisions).reduce((x, y) => x + y, 0);
  const tiles = [
    ["Volume traded", usd(a.volume, 0), `${a.fills || 0} fills`],
    ["Fees paid", usd(a.fees), a.volume ? pct(a.fees / a.volume, 2) + " of volume" : ""],
    ["Avg slippage", a.slippage.avg === null ? "–" : (a.slippage.avg * 100).toFixed(2) + "¢", `${a.slippage.count} entries`],
    ["Copy delay", a.latency.median === null ? "–" : num(a.latency.median, 1) + "s", a.latency.p90 === null ? "median" : `median · p90 ${num(a.latency.p90, 1)}s`],
    ["Signals copied", totalSignals ? pct((decisions.copied || 0) / totalSignals, 0) : "–", `${decisions.copied || 0} of ${totalSignals}`],
    ["Avg hold", a.avg_hold_hours === null ? "–" : num(a.avg_hold_hours, 1) + "h", `win rate ${m.win_rate === null || m.win_rate === undefined ? "–" : pct(m.win_rate, 0)}`],
  ];
  $("#analytics-tiles").innerHTML = tiles.map(([l, v, s]) => `<div class="tile"><div class="label">${esc(l)}</div><div class="value">${esc(v)}</div><div class="sub">${esc(s)}</div></div>`).join("");

  const leaders = a.leaders.slice(0, 15);
  polarityBars("leaders", $("#chart-leaders"), leaders.map((l) => l.name), leaders.map((l) => +(l.realized + l.unrealized).toFixed(2)),
    { horizontal: true, tooltipExtra: (i) => `${leaders[i].closed} closed, win ${leaders[i].win_rate === null ? "–" : pct(leaders[i].win_rate, 0)}` });
  const cats = a.categories;
  polarityBars("categories", $("#chart-categories"), cats.map((c) => c.category), cats.map((c) => +c.pnl.toFixed(2)), { horizontal: true });

  const b = a.price_buckets;
  const opts = baseOptions();
  opts.scales.y.min = 0; opts.scales.y.max = 1;
  opts.scales.y.ticks.callback = (v) => pct(v, 0);
  opts.scales.x.grid.display = false;
  opts.plugins.tooltip.callbacks = { label: (i) => ` win rate ${pct(i.raw, 0)} · ${b[i.dataIndex].positions} positions · ${signedUsd(b[i.dataIndex].pnl)}` };
  upsertChart("buckets", $("#chart-buckets"), {
    type: "bar",
    data: { labels: b.map((x) => x.bucket.split("-").map((p) => Math.round(p * 100) + "¢").join("–")), datasets: [{ data: b.map((x) => x.win_rate || 0), backgroundColor: css("--series-1"), borderRadius: 4, borderSkipped: "start", maxBarThickness: 24 }] },
    options: opts,
  });
  const h = a.slippage.histogram;
  const o2 = baseOptions();
  o2.scales.x.grid.display = false;
  o2.scales.y.ticks.precision = 0;
  o2.plugins.tooltip.callbacks = { label: (i) => ` ${i.raw} entries` };
  upsertChart("slippage", $("#chart-slippage"), {
    type: "bar",
    data: { labels: h.map((x) => x.label), datasets: [{ data: h.map((x) => x.n), backgroundColor: css("--series-1"), borderRadius: 4, borderSkipped: "start", maxBarThickness: 24 }] },
    options: o2,
  });
  $("#skip-table").innerHTML = a.skip_reasons.length
    ? `<thead><tr><th>Reason</th><th class="num">Signals</th></tr></thead><tbody>` + a.skip_reasons.map((r) => `<tr><td>${esc(r.reason)}</td><td class="num">${r.n}</td></tr>`).join("") + "</tbody>"
    : `<tr class="empty"><td>No skipped signals.</td></tr>`;
  $("#leader-score-table").innerHTML = a.leaders.length
    ? `<thead><tr><th>Leader</th><th class="num">Copies</th><th class="num">Closed</th><th class="num">Win</th><th class="num">Invested</th><th class="num">Realized</th><th class="num">ROI</th></tr></thead><tbody>` +
      a.leaders.map((l) => `<tr><td><a data-wallet="${esc(l.wallet)}">${esc(l.name)}</a></td><td class="num">${l.lots}</td><td class="num">${l.closed}</td><td class="num">${l.win_rate === null ? "–" : pct(l.win_rate, 0)}</td><td class="num">${usd(l.invested)}</td><td class="num ${deltaClass(l.realized)}">${signedUsd(l.realized)}</td><td class="num">${l.roi === null ? "–" : signedPct(l.roi)}</td></tr>`).join("") + "</tbody>"
    : `<tr class="empty"><td>No copies yet.</td></tr>`;
  $$("#leader-score-table a[data-wallet]").forEach((x) => x.addEventListener("click", () => openTrader(x.dataset.wallet)));
}

// ------------------------------------------------------------------ settings
const SCHEMA = [
  { key: "risk", title: "Position sizing & risk limits", fields: [
    ["bankroll_mode", "select", "Bankroll basis", "‘balance’ sizes from your whole equity. ‘fixed’ caps the bot at the amount below plus its own profits.", ["balance", "fixed"]],
    ["allocated_bankroll_usdc", "usd", "Fixed bankroll", "Used when bankroll basis is ‘fixed’."],
    ["per_trade_pct", "pct", "Base stake per copy", "Share of bankroll per copied trade before multipliers for leader score (0.6–1.4×), conviction (0.5–1.5×) and consensus (up to 1.5×)."],
    ["min_trade_usdc", "usd", "Minimum order", "Smaller stakes are skipped (Polymarket's minimum is $1)."],
    ["max_trade_usdc", "usd", "Maximum order", "Hard cap on any single copy."],
    ["max_market_exposure_pct", "pct", "Max per market", "Cap on money in one market (both outcomes)."],
    ["max_leader_exposure_pct", "pct", "Max per leader", "Cap on money copied from one trader."],
    ["max_total_exposure_pct", "pct", "Max invested", "Cap on total money in open positions."],
    ["cash_reserve_pct", "pct", "Cash reserve", "Always keep this share of the bankroll in cash."],
    ["max_open_positions", "int", "Max open positions", ""],
    ["daily_loss_limit_pct", "pct", "Daily loss limit", "Stop opening positions for the rest of the UTC day after losing this much."],
    ["max_drawdown_pause_pct", "pct", "Drawdown pause", "Pause new entries if the bot's equity falls this far below its peak."],
  ] },
  { key: "filters", title: "Which trades get copied", fields: [
    ["max_hours_to_resolution", "num", "Max hours until the market ends", "Your 2-day rule: only copy trades in markets scheduled to end within this many hours."],
    ["min_minutes_to_resolution", "num", "Min minutes until the market ends", "Ultra-short markets (e.g. 5–15 minute crypto) are dominated by millisecond bots; copies arrive too late."],
    ["min_entry_price", "cents", "Min entry price", "Skip long shots below this price."],
    ["max_entry_price", "cents", "Max entry price", "Skip near-certainties: little upside left after fees and slippage."],
    ["max_chase_cents", "cents", "Max chase", "Never pay more than the leader's price plus this…"],
    ["max_chase_pct", "pct", "Max chase (relative)", "…or more than this % above the leader's price (the tighter cap wins)."],
    ["max_spread", "cents", "Max bid/ask spread", "Skip illiquid books."],
    ["max_signal_age_sec", "num", "Max signal age (seconds)", "Ignore leader trades older than this when detected."],
    ["min_leader_trade_usdc", "usd", "Min leader trade size", "Ignore small, low-conviction leader trades."],
    ["min_market_liquidity_usdc", "usd", "Min market liquidity", ""],
    ["skip_in_play_sports", "bool", "Skip in-play sports", "Do not copy into games already in progress (prices move faster than a copy can follow)."],
    ["allow_adds", "bool", "Allow adding to a position", "Copy a leader's additional buys into a market we already hold (within limits)."],
    ["skip_conflicting_signals", "bool", "Skip conflicting signals", "Never hold both outcomes of the same market."],
    ["excluded_keywords", "list", "Excluded keywords", "Comma-separated. Markets whose title contains any of these are skipped (e.g. “Up or Down”)."],
  ] },
  { key: "exits", title: "Exits", fields: [
    ["mirror_leader_sells", "bool", "Mirror leader sells", "When a leader sells part of a position, sell the same fraction of what we copied from them."],
    ["exit_slippage_cents", "cents", "Exit price tolerance", "Accept up to this much below the leader's sell price."],
    ["exit_retry_minutes", "num", "Exit retry window (minutes)", "Keep retrying an exit this long, then hold to resolution."],
    ["take_profit_price", "optcents", "Take profit at", "Optional: sell when the price reaches this level instead of waiting for resolution (empty = off)."],
    ["auto_redeem", "bool", "Auto-claim winnings", "Redeem resolved winning positions back to USDC automatically (live mode)."],
  ] },
  { key: "discovery", title: "Trader discovery & scoring", fields: [
    ["interval_hours", "num", "Re-scan every (hours)", ""],
    ["leaderboard_windows", "multi", "Leaderboard windows", "", ["day", "week", "month", "all"]],
    ["leaderboard_categories", "list", "Extra category leaderboards", "Comma-separated Polymarket categories, e.g. sports, crypto, politics."],
    ["leaderboard_depth", "int", "Wallets per leaderboard", ""],
    ["max_candidates", "int", "Max wallets scored per scan", "Best-ranked wallets first; a scan of 300 takes a few minutes."],
    ["lookback_days", "int", "History to replay (days)", ""],
    ["min_resolved_positions", "int", "Min closed positions", "A trader needs at least this many copyable closed positions to qualify."],
    ["min_score", "num", "Min score to follow", "0–100."],
    ["max_followed", "int", "Max leaders followed", ""],
    ["sim_slippage_cents", "cents", "Simulated copy slippage", "How much worse than the leader the simulation assumes we fill."],
    ["max_trades_per_day", "num", "Max leader orders/day", "Higher-frequency wallets are bots or market makers — not copyable."],
    ["min_short_horizon_share", "pct", "Min short-dated share", "Share of a trader's volume that must be in markets matching your filters."],
    ["bench_live_roi", "pct", "Bench leader below live ROI", "Auto-unfollow a leader for 7 days when our real copies of them lose this much."],
    ["bench_min_positions", "int", "…after this many copies", ""],
  ] },
  { key: "execution", title: "Detection", fields: [
    ["use_realtime_stream", "bool", "Realtime trade feed", "Watch Polymarket's live trade stream for sub-second detection (polling always runs as backup)."],
    ["poll_interval_sec", "num", "Poll interval (seconds)", ""],
    ["aggregation_window_sec", "num", "Fill grouping window (seconds)", "Fills of one leader order are combined into one signal."],
  ] },
  { key: "", title: "Startup", fields: [
    ["auto_start", "bool", "Resume automatically", "When PolyCopy is relaunched, log in and resume the bot if it was running."],
  ] },
];

async function loadSettings() {
  const data = await api("/api/settings");
  app.settings = data.settings;
  app.presets = data.presets;
  app.defaults = data.defaults;
  renderSettingsForm();
  renderProfiles($("#profile-row"), app.settings.risk_profile, async (p) => {
    try { const r = await api("/api/settings/profile", { method: "POST", body: { profile: p } }); app.settings = r.settings; renderSettingsForm(); renderProfiles($("#profile-row"), p, null, true); toast(`Risk profile: ${p}`); }
    catch (e) { toast(e.message, true); }
  });
  $$("input[name=mode]").forEach((r) => { r.checked = r.value === (app.state ? app.state.mode : "paper"); });
  $("#paper-balance").value = app.settings.paper_starting_balance;
  renderAccountBox();
}

function renderProfiles(container, active, onPick, keepHandler) {
  const presets = app.presets || {};
  container.innerHTML = ["conservative", "balanced", "aggressive"].map((p) => {
    const v = presets[p] || {};
    return `<div class="profile-card ${p === active ? "active" : ""}" data-profile="${p}"><h4>${p}</h4>
      <div class="muted small">${{ conservative: "Small stakes, big cash buffer", balanced: "Recommended default", aggressive: "Larger stakes, faster growth & drawdowns" }[p]}</div>
      <ul><li>${pct(v.per_trade_pct, 0)} base stake, max ${usd(v.max_trade_usdc, 0)}</li><li>≤${pct(v.max_total_exposure_pct, 0)} invested · ${v.max_open_positions} positions</li><li>daily stop −${pct(v.daily_loss_limit_pct, 0)} · pause at −${pct(v.max_drawdown_pause_pct, 0)}</li></ul></div>`;
  }).join("") + (active === "custom" && container.id === "profile-row" ? `<div class="muted small">Current: custom limits</div>` : "");
  if (keepHandler) return;
  container._onPick = onPick;
  container.onclick = (ev) => {
    const card = ev.target.closest("[data-profile]");
    if (!card) return;
    $$(".profile-card", container).forEach((c) => c.classList.toggle("active", c === card));
    if (container._onPick) container._onPick(card.dataset.profile);
  };
}

function fieldValue(section, key) {
  const src = section ? app.settings[section] : app.settings;
  return src ? src[key] : undefined;
}

function renderSettingsForm() {
  const form = $("#settings-form");
  form.innerHTML = SCHEMA.map((sec) => `<div class="card settings-section"><div class="card-head"><h3>${esc(sec.title)}</h3></div><div class="settings-grid">` +
    sec.fields.map(([key, type, label, help, options]) => {
      const v = fieldValue(sec.key, key);
      const name = `${sec.key || "_"}.${key}`;
      let input;
      if (type === "bool") input = `<label class="check"><input type="checkbox" name="${name}" ${v ? "checked" : ""}> Enabled</label>`;
      else if (type === "select") input = `<select class="input" name="${name}">${options.map((o) => `<option ${o === v ? "selected" : ""}>${o}</option>`).join("")}</select>`;
      else if (type === "multi") input = `<div class="row">${options.map((o) => `<label class="check"><input type="checkbox" name="${name}" value="${o}" ${(v || []).includes(o) ? "checked" : ""}> ${o}</label>`).join("")}</div>`;
      else if (type === "list") input = `<input class="input" name="${name}" value="${esc((v || []).join(", "))}">`;
      else {
        let shown = v;
        let unit = "";
        if (type === "pct") { shown = v === null || v === undefined ? "" : +(v * 100).toFixed(3); unit = "%"; }
        else if (type === "cents" || type === "optcents") { shown = v === null || v === undefined ? "" : +(v * 100).toFixed(3); unit = "¢"; }
        else if (type === "usd") unit = "USDC";
        input = `<div class="row"><input class="input" style="flex:1" name="${name}" type="number" step="any" value="${shown ?? ""}">${unit ? `<span class="muted small">${unit}</span>` : ""}</div>`;
      }
      return `<label class="field" data-type="${type}"><span>${esc(label)}</span>${input}${help ? `<small class="muted">${esc(help)}</small>` : ""}</label>`;
    }).join("") + `</div></div>`).join("");
}

function collectSettings() {
  const patch = {};
  for (const sec of SCHEMA) {
    for (const [key, type] of sec.fields) {
      const name = `${sec.key || "_"}.${key}`;
      let value;
      if (type === "bool") value = $(`[name="${name}"]`).checked;
      else if (type === "multi") value = $$(`[name="${name}"]`).filter((i) => i.checked).map((i) => i.value);
      else if (type === "list") value = $(`[name="${name}"]`).value.split(",").map((s) => s.trim()).filter(Boolean);
      else if (type === "select") value = $(`[name="${name}"]`).value;
      else {
        const raw = $(`[name="${name}"]`).value.trim();
        if (raw === "") { if (type === "optcents") value = null; else continue; }
        else {
          value = Number(raw);
          if (type === "pct" || type === "cents" || type === "optcents") value = value / 100;
          if (type === "int") value = Math.round(value);
        }
      }
      if (sec.key) { patch[sec.key] = patch[sec.key] || {}; patch[sec.key][key] = value; } else patch[key] = value;
    }
  }
  return patch;
}

function renderAccountBox() {
  const s = app.state;
  const box = $("#account-box");
  if (!s) return;
  if (!s.account) {
    box.innerHTML = `<p class="muted">Not connected. Paper trading works without a wallet.</p><button class="btn btn-primary" id="acct-connect">Connect Polymarket wallet</button>`;
    $("#acct-connect").onclick = () => openWizard();
    return;
  }
  const a = s.account;
  box.innerHTML = `<div class="kv">
      <div><div class="k">Wallet</div><div class="v mono">${esc(shortAddr(a.wallet))}</div></div>
      <div><div class="k">Wallet type</div><div class="v">${esc(a.wallet_type)}</div></div>
      <div><div class="k">USDC balance</div><div class="v">${usd(a.cash)}</div></div>
      <div><div class="k">Signer</div><div class="v mono">${esc(shortAddr(a.signer))}</div></div>
      <div><div class="k">Approvals</div><div class="v">${esc(a.approvals || "–")}</div></div>
      <div><div class="k">Auto-claim</div><div class="v">${a.has_relayer_key ? "enabled" : "manual"}</div></div>
    </div><p class="muted small">Credentials stored in: ${esc(s.secret_backend)}.</p>
    <button class="btn btn-danger" id="acct-logout">Log out &amp; forget key</button>`;
  $("#acct-logout").onclick = async () => {
    if (!confirm("Log out and remove the stored key from this Mac? Live trading stops.")) return;
    try { await api("/api/logout", { method: "POST" }); toast("Logged out"); refreshState(); } catch (e) { toast(e.message, true); }
  };
}

// ------------------------------------------------------------------ logs
async function loadLogs() {
  const q = app.logFilter ? `&level=${app.logFilter}` : "";
  const rows = await api(`/api/logs?limit=500${q}`);
  const el = $("#logs-table");
  if (!rows.length) { el.innerHTML = `<tr class="empty"><td>No log entries.</td></tr>`; return; }
  el.innerHTML = `<thead><tr><th>Time</th><th>Level</th><th>Area</th><th>Message</th></tr></thead><tbody>` +
    rows.map((r) => `<tr><td class="muted">${timeOf(r.ts)}</td><td>${r.level === "error" ? tag("error", "bad") : r.level === "warning" ? tag("warning", "warn") : tag(r.level, "info")}</td><td class="muted">${esc(r.category || "")}</td><td>${esc(r.message)}</td></tr>`).join("") + "</tbody>";
}

// ------------------------------------------------------------------ help
function renderHelp() {
  $("#help-content").innerHTML = `
  <h2>How PolyCopy works</h2>
  <ol>
    <li><strong>Discover.</strong> Every few hours PolyCopy reads Polymarket's profit leaderboards (overall, sports, crypto) and replays each candidate's last 30 days of trades <em>as if you had copied them</em>: same filters you set, a worse price than the trader got, Polymarket's taker fees, and mirrored exits. Traders are scored on that simulated copy profit, statistical confidence that it is skill (not luck), consistency and drawdown.</li>
    <li><strong>Follow.</strong> The best-scoring traders (up to the limit in Settings) are followed. You can follow, unfollow or block anyone on the Traders tab.</li>
    <li><strong>Detect.</strong> A realtime trade feed plus polling spots a followed trader's trade within seconds.</li>
    <li><strong>Filter.</strong> The trade is only copied if the market ends within your window (48h by default), the price has not run away from what the leader paid, the book has liquidity, and your risk limits allow it. Every skipped trade is listed with its reason on the Signals tab.</li>
    <li><strong>Copy & manage.</strong> PolyCopy buys with a price cap, sells when the leader sells (same fraction), and settles at resolution, claiming winnings automatically.</li>
  </ol>
  <h2>Day-to-day</h2>
  <ul>
    <li><strong>Start / Pause / Stop</strong> (top right). Pause stops new entries but keeps managing open positions. Stop halts everything.</li>
    <li><strong>Paper vs Live.</strong> Paper uses the real order book with simulated money. Switch in Settings once paper results satisfy you.</li>
    <li><strong>Close a position</strong> from the Positions tab, or close all of them at once.</li>
    <li>Keep the PolyCopy window open (and the Mac awake) while it trades. Closing it stops the bot; it resumes automatically next launch.</li>
  </ul>
  <h2>Reading the numbers</h2>
  <ul>
    <li><strong>Score</strong> (0–100): simulated copy return shrunk for small samples × confidence × consistency, with penalties for concentrated wins and drawdowns.</li>
    <li><strong>Skill z</strong>: how many standard deviations a trader's win count beats what their entry prices implied. Above 2 is strong evidence of skill.</li>
    <li><strong>Slippage</strong>: cents you paid above the leader's price. <strong>Delay</strong>: seconds from the leader's trade to detection.</li>
  </ul>
  <p class="muted">Full documentation: docs/USER_GUIDE.md and docs/STRATEGY.md in the PolyCopy folder.</p>`;
}

// ------------------------------------------------------------------ wizard
function openWizard(step = 1) {
  $("#wizard").classList.remove("hidden");
  showWizardPage(step);
  renderProfiles($("#wz-profiles"), (app.settings && app.settings.risk_profile !== "custom") ? app.settings.risk_profile : "balanced", (p) => { app.wizardProfile = p; });
  app.wizardProfile = (app.settings && app.settings.risk_profile !== "custom") ? app.settings.risk_profile : "balanced";
}
function closeWizard() { $("#wizard").classList.add("hidden"); }
function showWizardPage(n) {
  $$(".wizard-page").forEach((p) => p.classList.toggle("hidden", p.dataset.page !== String(n)));
  $$(".wizard-steps span").forEach((s) => s.classList.toggle("active", Number(s.dataset.step) <= n));
  if (n === 3) {
    const live = $("#wz-live-card");
    const can = app.state && app.state.logged_in;
    live.style.opacity = can ? 1 : 0.5;
    $("input[value=live]", live).disabled = !can;
    if (!can) $("input[name=wz-mode][value=paper]").checked = true;
    updateAckRow();
  }
}
function updateAckRow() {
  const live = $("input[name=wz-mode]:checked").value === "live";
  $("#wz-ack-row").classList.toggle("hidden", !live);
}
function wizardError(id, message) {
  const el = $(id);
  el.textContent = message || "";
  el.classList.toggle("hidden", !message);
}

function bindWizard() {
  $("#wz-detect").onclick = async () => {
    const key = $("#wz-key").value.trim();
    if (!key) { wizardError("#wz-error", "Paste your private key first."); return; }
    wizardError("#wz-error", "");
    $("#wz-candidates").innerHTML = `<p class="muted small">Checking wallets derived from this key…</p>`;
    try {
      const r = await api("/api/wallet/detect", { method: "POST", body: { private_key: key } });
      const c = r.candidates || [];
      if (!c.length) { $("#wz-candidates").innerHTML = `<p class="muted small">Detection is not available here; paste the address from your Polymarket profile.</p>`; return; }
      $("#wz-candidates").innerHTML = c.map((x, i) => `<div class="candidate" data-addr="${esc(x.address)}"><div><strong class="mono">${esc(shortAddr(x.address))}</strong> <span class="muted small">${esc(x.wallet_type)}</span></div><div class="small">${x.last_activity ? "active " + ago(x.last_activity) : "no activity"} · ${usd(x.portfolio_value)} in positions ${i === 0 && x.last_activity ? tag("best match", "good") : ""}</div></div>`).join("");
      $$(".candidate").forEach((el) => el.addEventListener("click", () => { $("#wz-wallet").value = el.dataset.addr; }));
      if (c[0].last_activity) $("#wz-wallet").value = c[0].address;
    } catch (e) { $("#wz-candidates").innerHTML = ""; wizardError("#wz-error", e.message); }
  };
  $("#wz-login").onclick = async () => {
    const key = $("#wz-key").value.trim();
    const wallet = $("#wz-wallet").value.trim();
    if (!key) { wizardError("#wz-error", "Paste your private key."); return; }
    if (!wallet && !(app.state && app.state.demo)) { wizardError("#wz-error", "Enter your Polymarket wallet address (or press Detect)."); return; }
    const btn = $("#wz-login");
    btn.disabled = true; btn.textContent = "Logging in…";
    wizardError("#wz-error", "");
    try {
      await api("/api/login", { method: "POST", body: { private_key: key, wallet: wallet || null, remember: $("#wz-remember").checked } });
      $("#wz-key").value = "";
      await refreshState();
      toast("Logged in to Polymarket");
      showWizardPage(2);
    } catch (e) { wizardError("#wz-error", e.message); }
    finally { btn.disabled = false; btn.textContent = "Log in"; }
  };
  $("#wz-skip").onclick = () => showWizardPage(2);
  $$("[data-back]").forEach((b) => (b.onclick = () => showWizardPage(Number(b.dataset.back))));
  $$("[data-next]").forEach((b) => (b.onclick = () => showWizardPage(Number(b.dataset.next))));
  $$("input[name=wz-mode]").forEach((r) => r.addEventListener("change", updateAckRow));
  $("#wz-start").onclick = async () => {
    const mode = $("input[name=wz-mode]:checked").value;
    if (mode === "live" && !$("#wz-ack").checked) { wizardError("#wz-error3", "Please confirm the risk acknowledgement to trade live."); return; }
    wizardError("#wz-error3", "");
    try {
      await api("/api/settings/profile", { method: "POST", body: { profile: app.wizardProfile || "balanced" } });
      const bankroll = $("input[name=wz-bankroll]:checked").value;
      await api("/api/settings", { method: "PUT", body: { risk: { bankroll_mode: bankroll, allocated_bankroll_usdc: Number($("#wz-fixed").value) || 500 } } });
      await api("/api/mode", { method: "POST", body: { mode } });
      await api("/api/bot/start", { method: "POST" });
      store.set("onboarded", true);
      closeWizard();
      toast(mode === "live" ? "Live trading started" : "Paper trading started");
      await refreshState();
      loadSettings();
    } catch (e) { wizardError("#wz-error3", e.message); }
  };
  $("#wizard").addEventListener("click", (ev) => { if (ev.target.id === "wizard" && store.get("onboarded", false)) closeWizard(); });
}

// ------------------------------------------------------------------ navigation & refresh
function setTab(tab) {
  app.tab = tab;
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.tab === tab));
  $$(".tab").forEach((s) => s.classList.toggle("active", s.id === `tab-${tab}`));
  store.set("tab", tab);
  refreshActive(true);
}

async function refreshState() {
  try {
    app.state = await api("/api/state");
    renderHeader();
    if (app.tab === "dashboard") renderTiles();
  } catch (e) { /* server restarting */ }
}

let refreshing = false;
async function refreshActive(force = false) {
  if (refreshing && !force) return;
  refreshing = true;
  try {
    switch (app.tab) {
      case "dashboard": await loadDashboard(); break;
      case "positions": await loadPositions(); break;
      case "orders": await loadOrders(); break;
      case "signals": await loadSignals(); break;
      case "traders": await loadTraders(); break;
      case "analytics": await loadAnalytics(); break;
      case "settings": if (force) await loadSettings(); else renderAccountBox(); break;
      case "logs": await loadLogs(); break;
      case "help": renderHelp(); break;
    }
  } catch (e) {
    console.warn(e);
  } finally { refreshing = false; }
}

// ------------------------------------------------------------------ websocket
let ws = null;
let pendingRefresh = null;
function scheduleRefresh() {
  if (pendingRefresh) return;
  pendingRefresh = setTimeout(() => { pendingRefresh = null; refreshActive(); }, 600);
}
function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/api/ws`);
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    switch (msg.type) {
      case "state":
        app.state = msg.data; renderHeader(); if (app.tab === "dashboard") renderTiles(); if (app.tab === "settings") renderAccountBox();
        break;
      case "portfolio":
        if (app.state) { app.state.summary = msg.data; if (app.tab === "dashboard") renderTiles(); }
        break;
      case "discovery":
        if (app.state) { app.state.discovery = msg.data; renderDiscovery(); renderBanners(); }
        if (!msg.data.running) { refreshState(); if (app.tab === "traders") loadTraders(); }
        break;
      case "signal":
        if (app.tab === "dashboard") { const feed = $("#dash-feed"); if (feed.querySelector("li span.muted")) feed.innerHTML = ""; feed.insertAdjacentHTML("afterbegin", feedItem(msg.data)); }
        if (["signals", "positions"].includes(app.tab)) scheduleRefresh();
        if (msg.data.decision === "copied") toast(`Copied ${msg.data.leader_name || shortAddr(msg.data.wallet)}: ${msg.data.outcome || ""} @ ${cents(msg.data.leader_price)}`);
        break;
      case "order":
        if (["orders", "positions", "dashboard"].includes(app.tab)) scheduleRefresh();
        break;
      case "traders":
        refreshState(); if (app.tab === "traders") scheduleRefresh();
        break;
      case "log":
        if (app.tab === "logs") scheduleRefresh();
        if (msg.data.level === "error") toast(msg.data.message, true);
        break;
    }
  };
  ws.onclose = () => setTimeout(connectWs, 2000);
}

// ------------------------------------------------------------------ boot
function bindUi() {
  $$("#nav a").forEach((a) => a.addEventListener("click", () => setTab(a.dataset.tab)));
  $$("[data-goto]").forEach((a) => a.addEventListener("click", () => setTab(a.dataset.goto)));
  $("#theme-toggle").onclick = () => applyTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark");
  $("#btn-start").onclick = async () => {
    try {
      if (app.state && app.state.mode === "live" && !app.state.logged_in) { openWizard(1); return; }
      app.state = await api("/api/bot/start", { method: "POST" }); renderHeader(); toast("Bot running");
    } catch (e) { toast(e.message, true); }
  };
  $("#btn-pause").onclick = async () => { try { app.state = await api("/api/bot/pause", { method: "POST" }); renderHeader(); toast("New entries paused"); } catch (e) { toast(e.message, true); } };
  $("#btn-stop").onclick = async () => {
    if (!confirm("Stop the bot? Open positions stay open but are no longer managed until you start again.")) return;
    try { app.state = await api("/api/bot/stop", { method: "POST" }); renderHeader(); toast("Bot stopped"); } catch (e) { toast(e.message, true); }
  };
  $("#wallet-chip").onclick = () => { if (app.state && app.state.account) setTab("settings"); else openWizard(1); };
  $("#equity-range").addEventListener("click", (ev) => {
    const b = ev.target.closest("button"); if (!b) return;
    $$("#equity-range button").forEach((x) => x.classList.toggle("active", x === b));
    app.equityDays = Number(b.dataset.days);
    api(`/api/equity?days=${app.equityDays}`).then(drawEquity);
  });
  const segBind = (id, attr, prop, loader) => $(id).addEventListener("click", (ev) => {
    const b = ev.target.closest("button"); if (!b) return;
    $$(`${id} button`).forEach((x) => x.classList.toggle("active", x === b));
    app[prop] = b.dataset[attr]; loader();
  });
  segBind("#pos-filter", "status", "positionsStatus", loadPositions);
  segBind("#signal-filter", "decision", "signalFilter", loadSignals);
  segBind("#trader-filter", "status", "traderFilter", loadTraders);
  segBind("#log-filter", "level", "logFilter", loadLogs);
  $("#btn-close-all").onclick = async () => {
    if (!confirm("Sell ALL open positions at the current bids?")) return;
    try { const r = await api("/api/positions/close-all", { method: "POST" }); toast(`Sold ${r.sold}, pending ${r.pending}, failed ${r.failed}`); loadPositions(); } catch (e) { toast(e.message, true); }
  };
  $("#btn-add-trader").onclick = async () => {
    const w = $("#add-trader").value.trim();
    if (!/^0x[0-9a-fA-F]{40}$/.test(w)) { toast("Enter a valid 0x wallet address", true); return; }
    try { await api("/api/traders", { method: "POST", body: { wallet: w } }); $("#add-trader").value = ""; toast("Following wallet; scoring it in the background"); loadTraders(); refreshState(); } catch (e) { toast(e.message, true); }
  };
  $("#btn-discovery").onclick = async () => {
    try { const p = await api("/api/discovery/run", { method: "POST" }); if (app.state) { app.state.discovery = p; renderDiscovery(); } toast("Scanning leaderboards…"); } catch (e) { toast(e.message, true); }
  };
  $("#btn-settings-save").onclick = async () => {
    try {
      const r = await api("/api/settings", { method: "PUT", body: collectSettings() });
      app.settings = r.settings; $("#settings-status").textContent = "Saved " + new Date().toLocaleTimeString();
      renderProfiles($("#profile-row"), app.settings.risk_profile, null, true); toast("Settings saved");
    } catch (e) { toast(e.message, true); }
  };
  $("#btn-settings-reset").onclick = async () => {
    if (!confirm("Restore every setting to its default value?")) return;
    const d = Object.assign({}, app.defaults); delete d.mode;
    try { const r = await api("/api/settings", { method: "PUT", body: d }); app.settings = r.settings; renderSettingsForm(); toast("Defaults restored"); loadSettings(); } catch (e) { toast(e.message, true); }
  };
  $("#btn-paper-reset").onclick = async () => {
    const bal = Number($("#paper-balance").value);
    if (!(bal > 0)) { toast("Enter a starting balance", true); return; }
    if (!confirm(`Reset the paper account to ${usd(bal)}? Paper history is deleted.`)) return;
    try { app.state = await api("/api/paper/reset", { method: "POST", body: { balance: bal } }); renderHeader(); toast("Paper account reset"); } catch (e) { toast(e.message, true); }
  };
  $$("input[name=mode]").forEach((r) => r.addEventListener("change", async () => {
    const mode = r.value;
    if (mode === "live" && !confirm("Switch to LIVE trading with real money from your Polymarket wallet?")) { loadSettings(); return; }
    try { app.state = await api("/api/mode", { method: "POST", body: { mode } }); renderHeader(); toast(`Switched to ${mode} mode`); }
    catch (e) { toast(e.message, true); loadSettings(); if (/log in/i.test(e.message)) openWizard(1); }
  }));
  $("#btn-quit").onclick = async () => {
    if (!confirm("Quit the PolyCopy server? The bot stops until you launch PolyCopy again.")) return;
    try { await api("/api/shutdown", { method: "POST" }); document.body.innerHTML = '<div style="padding:40px;font:16px system-ui">PolyCopy has stopped. Launch it again to resume. You can close this tab.</div>'; }
    catch (e) { toast(e.message, true); }
  };
  $("#drawer-close").onclick = closeDrawer;
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeDrawer(); });
  bindWizard();
}

async function boot() {
  const theme = store.get("theme", "dark");
  document.documentElement.setAttribute("data-theme", theme);
  bindUi();
  await refreshState();
  try { const s = await api("/api/settings"); app.settings = s.settings; app.presets = s.presets; app.defaults = s.defaults; } catch { /* ignore */ }
  setTab(store.get("tab", "dashboard"));
  connectWs();
  setInterval(refreshState, 15000);
  setInterval(() => { if (!["settings", "help"].includes(app.tab)) refreshActive(); }, 10000);
  if (app.state && !app.state.logged_in && !store.get("onboarded", false) && app.state.status === "stopped") openWizard(1);
}

boot();
