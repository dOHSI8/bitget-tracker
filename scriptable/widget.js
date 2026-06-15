// Bitget Tracker — Scriptable widget
// Works in: home screen widget, lock screen widget, Scriptable app, iOS Shortcut

const BASE_URL = "https://YOUR-SERVICE-NAME.onrender.com";

const GREEN = new Color("#00c47a");
const RED   = new Color("#ff4d4d");
const AMBER = new Color("#f59e0b");
const WHITE = new Color("#ffffff");
const MUTED = new Color("#888888");
const BG    = new Color("#111111");
const SEP_C = new Color("#333333");

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

// ── Helpers ───────────────────────────────────────────────────────────────────
function txt(stack, content, size, color, bold = false) {
  const t = stack.addText(content);
  t.font = bold ? Font.boldSystemFont(size) : Font.systemFont(size);
  t.textColor = color;
  t.lineLimit = 1;
  t.minimumScaleFactor = 0.7;
  return t;
}

function fmtUSD(n) {
  if (n == null) return "$0";
  return "$" + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtPnL(n) {
  return (n >= 0 ? "+" : "-") + fmtUSD(n);
}

function fmtShort(n) {
  if (n == null) return "$0";
  const abs = Math.abs(n);
  if (abs >= 1000) return (n >= 0 ? "+" : "-") + "$" + (abs / 1000).toFixed(1) + "k";
  return (n >= 0 ? "+" : "-") + "$" + abs.toFixed(2);
}

// ── Main ──────────────────────────────────────────────────────────────────────
const { data, stale } = await fetchData();

if (!data) {
  const w = new ListWidget();
  w.backgroundColor = BG;
  const t = w.addText("⚠ No data — check server");
  t.font = Font.boldSystemFont(11);
  t.textColor = AMBER;
  Script.setWidget(w);
  Script.complete();
  return;
}

const pnl    = data.daily_pnl ?? 0;
const bal    = data.total_balance ?? 0;
const inv    = data.total_investment ?? 0;
const nPos   = data.open_positions ?? 0;
const oPnl   = data.open_positions_pnl ?? 0;
const allPnl = data.all_time_pnl ?? 0;
const updAt  = data.updated_at ?? "--:--";
const pnlColor = pnl >= 0 ? GREEN : RED;

const family = config.widgetFamily;

// ═══════════════════════════════════════════════════════════════════════════
// LOCK SCREEN — accessoryRectangular
// ═══════════════════════════════════════════════════════════════════════════
if (family === "accessoryRectangular") {
  const lw = new ListWidget();
  lw.setPadding(0, 0, 0, 0);
  lw.refreshAfterDate = new Date(Date.now() + 2 * 60 * 1000);

  const pnlStr  = fmtPnL(pnl);
  const oPnlStr = fmtPnL(oPnl);
  const allStr  = fmtPnL(allPnl);
  const oPnlC   = oPnl >= 0 ? GREEN : RED;
  const allC    = allPnl >= 0 ? GREEN : RED;
  const posLabel = nPos === 1 ? "1 position" : nPos + " positions";

  const r1 = lw.addStack(); r1.layoutHorizontally(); r1.centerAlignContent();
  txt(r1, "Bal  " + fmtUSD(bal), 12, WHITE, true);
  r1.addSpacer();
  if (stale) { txt(r1, "stale", 10, AMBER); }

  lw.addSpacer(1);

  const r2 = lw.addStack(); r2.layoutHorizontally(); r2.centerAlignContent();
  txt(r2, "Today  ", 10, MUTED);
  txt(r2, pnlStr, 12, pnlColor, true);

  lw.addSpacer(1);

  const r3 = lw.addStack(); r3.layoutHorizontally(); r3.centerAlignContent();
  txt(r3, "Open " + oPnlStr + "  |  " + posLabel, 10, oPnlC);

  lw.addSpacer(1);

  const r4 = lw.addStack(); r4.layoutHorizontally(); r4.centerAlignContent();
  txt(r4, "All time  ", 10, MUTED);
  txt(r4, allStr, 12, allC, true);

  Script.setWidget(lw);
  Script.complete();
  return;
}

// ═══════════════════════════════════════════════════════════════════════════
// HOME SCREEN — medium (default)
// ═══════════════════════════════════════════════════════════════════════════
const widget = new ListWidget();
widget.backgroundColor = BG;
widget.setPadding(14, 14, 14, 14);
widget.refreshAfterDate = new Date(Date.now() + 2 * 60 * 1000);

// Row 1: Header
const row1 = widget.addStack();
row1.layoutHorizontally();
row1.centerAlignContent();
txt(row1, "BITGET", 10, MUTED, true);
row1.addSpacer(4);
txt(row1, "· DKTrading", 10, MUTED);
row1.addSpacer();
if (stale) { txt(row1, "⚠", 10, AMBER); row1.addSpacer(2); }
txt(row1, updAt, 10, MUTED);

widget.addSpacer(6);

// Row 2: Balance & Investment
const row2 = widget.addStack();
row2.layoutHorizontally();

const balCol = row2.addStack(); balCol.layoutVertically();
txt(balCol, "BALANCE", 8, MUTED);
txt(balCol, fmtUSD(bal), 16, WHITE, true);

row2.addSpacer();

const invCol = row2.addStack(); invCol.layoutVertically();
txt(invCol, "INVESTED", 8, MUTED);
txt(invCol, fmtUSD(inv), 16, WHITE, true);

widget.addSpacer(6);

// Separator
const sepRow = widget.addStack();
const sep = sepRow.addText("─────────────────────────────");
sep.font = Font.systemFont(6);
sep.textColor = SEP_C;

widget.addSpacer(6);

// Row 3: Daily PnL
const row3 = widget.addStack();
row3.layoutHorizontally();
row3.centerAlignContent();
txt(row3, "TODAY", 8, MUTED);
row3.addSpacer(8);
txt(row3, fmtPnL(pnl), 20, pnlColor, true);
row3.addSpacer();

widget.addSpacer(6);

// Row 4: Open PnL | Positions | All-time
const row4 = widget.addStack();
row4.layoutHorizontally();

const col1 = row4.addStack(); col1.layoutVertically();
txt(col1, "OPEN P&L", 8, MUTED);
txt(col1, fmtPnL(oPnl), 11, oPnl >= 0 ? GREEN : RED, true);

row4.addSpacer();

const col2 = row4.addStack(); col2.layoutVertically();
txt(col2, "POS", 8, MUTED);
txt(col2, String(nPos), 11, WHITE, true);

row4.addSpacer();

const col3 = row4.addStack(); col3.layoutVertically();
txt(col3, "ALL-TIME", 8, MUTED);
txt(col3, fmtPnL(allPnl), 11, allPnl >= 0 ? GREEN : RED, true);

Script.setWidget(widget);
Script.complete();
