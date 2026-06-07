// Bitget Tracker — Scriptable widget

const BASE_URL = "https://bitget-tracker-v2.onrender.com";

// Palette — mirrors the web dashboard
const BG    = new Color("#080c08");
const TEXT  = new Color("#dde8dd");
const MUTED = new Color("#4a5e4a");
const GREEN = new Color("#00c47a");
const RED   = new Color("#e84040");
const AMBER = new Color("#d4880a");

// ── Fetch ─────────────────────────────────────────────────────────────────────
async function fetchData() {
  const req = new Request(`${BASE_URL}/api/widget`);
  req.timeoutInterval = 30;
  try {
    const data = await req.loadJSON();
    Keychain.set("bitget_widget_cache", JSON.stringify(data));
    return { data, stale: data.stale || false };
  } catch (e) {
    const cached = Keychain.contains("bitget_widget_cache")
      ? JSON.parse(Keychain.get("bitget_widget_cache"))
      : null;
    return { data: cached, stale: true };
  }
}

// ── Typography ────────────────────────────────────────────────────────────────
// For all numeric values — monospaced digits align cleanly (functional, not decorative)
function digits(parent, str, size, color = TEXT, bold = false) {
  const t = parent.addText(str);
  t.font = bold ? Font.boldSystemFont(size) : Font.systemFont(size);
  t.textColor = color;
  t.lineLimit = 1;
  t.minimumScaleFactor = 0.8;
  return t;
}

// For labels and copy
function note(parent, str, size = 9, color = MUTED) {
  const t = parent.addText(str);
  t.font = Font.systemFont(size);
  t.textColor = color;
  t.lineLimit = 1;
  return t;
}

// ── Formatters ────────────────────────────────────────────────────────────────
function fmtUSD(n) {
  if (n == null) return "$0.00";
  return "$" + Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: 2, maximumFractionDigits: 2
  });
}

function fmtPnL(n) {
  if (n == null || n === 0) return "$0.00";
  return (n > 0 ? "+" : "−") + fmtUSD(n);
}

function pnlColor(n) { return n > 0 ? GREEN : n < 0 ? RED : MUTED; }

// ── Main ──────────────────────────────────────────────────────────────────────
const { data, stale } = await fetchData();

if (!data) {
  const w = new ListWidget();
  w.backgroundColor = BG;
  w.setPadding(12, 12, 12, 12);
  note(w, "no data · check server", 11, AMBER);
  Script.setWidget(w);
  Script.complete();
  return;
}

const pnl    = data.daily_pnl          ?? 0;
const bal    = data.total_balance      ?? 0;
const inv    = data.total_investment   ?? 0;
const nPos   = data.open_positions     ?? 0;
const oPnl   = data.open_positions_pnl ?? 0;
const allPnl = data.all_time_pnl       ?? 0;
const updAt  = data.updated_at         ?? "--:--";
const family = config.widgetFamily;

// ══════════════════════════════════════════════════════════════════════════════
// LOCK SCREEN — accessoryRectangular
// ══════════════════════════════════════════════════════════════════════════════
if (family === "accessoryRectangular") {
  const lw = new ListWidget();
  lw.setPadding(0, 0, 0, 0);
  lw.refreshAfterDate = new Date(Date.now() + 2 * 60 * 1000);

  const rows = [
    ["bal",                fmtUSD(bal),    TEXT],
    ["today",              fmtPnL(pnl),    pnlColor(pnl)],
    ["open · " + nPos + " pos", fmtPnL(oPnl), pnlColor(oPnl)],
    ["∑",                  fmtPnL(allPnl), pnlColor(allPnl)],
  ];

  rows.forEach(([lbl, val, c], i) => {
    if (i > 0) lw.addSpacer(2);
    const row = lw.addStack();
    row.layoutHorizontally();
    row.centerAlignContent();
    note(row, lbl + "  ", 10);
    digits(row, val, 11, c, true);
    if (i === 0 && stale) {
      row.addSpacer(4);
      note(row, "·", 10, AMBER);
    }
  });

  Script.setWidget(lw);
  Script.complete();
  return;
}

// ══════════════════════════════════════════════════════════════════════════════
// SMALL — 2-col top (balance | all-time), today below
// ══════════════════════════════════════════════════════════════════════════════
if (family === "small") {
  const sw = new ListWidget();
  sw.backgroundColor = BG;
  sw.setPadding(14, 14, 12, 14);
  sw.refreshAfterDate = new Date(Date.now() + 2 * 60 * 1000);

  // Top: balance (left) | all-time PnL (right) — fills horizontal space
  const topRow = sw.addStack();
  topRow.layoutHorizontally();

  const balCol = topRow.addStack(); balCol.layoutVertically();
  note(balCol, "balance", 8);
  balCol.addSpacer(4);
  digits(balCol, fmtUSD(bal), 18, TEXT, true);

  topRow.addSpacer();

  const atCol = topRow.addStack(); atCol.layoutVertically();
  note(atCol, "all-time", 8);
  atCol.addSpacer(4);
  digits(atCol, fmtPnL(allPnl), 13, pnlColor(allPnl), true);

  sw.addSpacer(10);

  // Today PnL
  note(sw, "today", 8);
  sw.addSpacer(4);
  digits(sw, fmtPnL(pnl), 20, pnlColor(pnl), true);

  // Timestamp pinned to bottom
  sw.addSpacer();
  note(sw, updAt + (stale ? " · stale" : ""), 8, stale ? AMBER : MUTED);

  Script.setWidget(sw);
  Script.complete();
  return;
}

// ══════════════════════════════════════════════════════════════════════════════
// MEDIUM (default) — full layout
// ══════════════════════════════════════════════════════════════════════════════
const widget = new ListWidget();
widget.backgroundColor = BG;
widget.setPadding(14, 14, 12, 14);
widget.refreshAfterDate = new Date(Date.now() + 2 * 60 * 1000);

// Compute all-time return % for the sub-label
const atBase   = inv > 0 ? inv : bal;
const atRetPct = atBase > 0 ? (allPnl / atBase * 100) : null;
const atRetStr = atRetPct != null
  ? (atRetPct >= 0 ? "+" : "") + atRetPct.toFixed(1) + "%"
  : "—";

// ── Row 1: trader name left · timestamp right
const rTop = widget.addStack();
rTop.layoutHorizontally();
rTop.centerAlignContent();
note(rTop, "Bitget · all portfolios");
rTop.addSpacer();
note(rTop, stale ? "· stale  " + updAt : updAt, 9, stale ? AMBER : MUTED);

widget.addSpacer(6);

// ── Row 2: balance large (fills width)
digits(widget, fmtUSD(bal), 28, TEXT, true);

// ── Row 3: invested · all-time return — context under the balance
const invLabel = inv > 0 ? fmtUSD(inv) + " invested" : "all-profit";
note(widget, invLabel + "  ·  " + atRetStr + " return", 9, MUTED);

widget.addSpacer(8);

// ── Row 4: today PnL — the emotional center
const rToday = widget.addStack();
rToday.layoutHorizontally();
rToday.centerAlignContent();
note(rToday, "today  ");
digits(rToday, fmtPnL(pnl), 22, pnlColor(pnl), true);
rToday.addSpacer();

widget.addSpacer(8);

// ── Row 5: open · positions · all-time
const rBtm = widget.addStack();
rBtm.layoutHorizontally();
rBtm.centerAlignContent();

const ocol = rBtm.addStack(); ocol.layoutVertically();
note(ocol, "open");
digits(ocol, fmtPnL(oPnl), 11, pnlColor(oPnl), true);

rBtm.addSpacer();

const pcol = rBtm.addStack(); pcol.layoutVertically();
note(pcol, "positions");
const posNum = pcol.addText(String(nPos));
posNum.font = Font.boldSystemFont(11);
posNum.textColor = nPos > 0 ? GREEN : MUTED;

rBtm.addSpacer();

const acol = rBtm.addStack(); acol.layoutVertically();
note(acol, "all-time");
digits(acol, fmtPnL(allPnl), 11, pnlColor(allPnl), true);

Script.setWidget(widget);
Script.complete();
