# Bitget Copy Trading Tracker — Project Context

## What this is
FastAPI backend + Playwright scraper + iPhone Scriptable widget.
Tracks a **copy trading FOLLOWER** account on Bitget (following trader "DKTrading").
Deployed on Render free tier: `https://YOUR-SERVICE-NAME.onrender.com`

## Architecture
- `main.py` — FastAPI app, in-memory state (`_mt5`, `_settings`), `/api/widget`, `/api/poller`
- `browser_poller.py` — Playwright/Chromium headless browser, polls every 2 min
- `scriptable/widget.js` — iPhone home screen widget (Scriptable app)
- `cookies.json` — Bitget session cookie (auto-restored from `BITGET_COOKIE` env var on redeploy)

## How scraping works
No API keys. Uses Playwright to navigate to `bitget.com/about`, inject session cookies,
then call Bitget's internal APIs via `page.evaluate(fetch(...))` with `credentials: 'include'`.

## Working endpoints (confirmed)
| Data | Endpoint | Notes |
|------|----------|-------|
| Balance / investment / all-time PnL | `POST /v1/trace/mt5/trace/getFollowPortfolios` | Body: `{portfolioId}` → `data.portfolioDetails[0]` |
| Closed trade history | `POST /v1/trace/mt5/trace/positionHistory` | Body: `{portfolioId, pageNo:1, pageSize:50}` |

## Not working / unknown
- **Open positions endpoint**: all guesses return 403 or 404. `getFollowOpenPosition` returns 403 (exists, wrong auth). To find it: use Proxyman on iPhone when trader has open positions.
- **Balance history**: all candidates 404

## Cookie management
- User copies `document.cookie` from Chrome DevTools console on `bitget.com` while logged in
- Pastes into dashboard → Polling Setup → saved to `cookies.json`
- Cookie has ~5 day TTL (`bt_newsessionid` JWT). Re-paste when polls start failing.
- `cookie_preview` in `/api/poller` shows first 40 chars (starts with `__cf_bm=...` which is normal)

## Staleness detection
`pushed_at` (HH:MM) is set whenever `positions` OR `copy_details` is pushed.
Widget returns `stale: true` if `pushed_at` is >15 min old.
Since balance scrapes every 2 min, stale should never trigger unless cookie expired.

## iPhone widget
- `scriptable/widget.js` — hardcoded URL, no setup prompt, runs silently
- Timeout: 30s, `refreshAfterDate`: 2 min
- iOS auto-refreshes widget in background (~every 5–15 min)
- Shortcuts automation on charging: "Run Script" with **"Run in App" = OFF** (silent)

## Render deploy
- Auto-deploys from `master` branch
- Free tier sleeps after 15 min inactivity → use UptimeRobot to ping every 10 min
- In-memory state resets on redeploy; `settings.json` persists investment/balance overrides

## Key fields in `_mt5` / `_settings`
```
_mt5:      positions_raw, history_raw, summary, pushed_at
_settings: balance, investment, all_time_pnl, realized_pnl
```

## Useful endpoints
- `/api/widget` — widget data (balance, pnl, stale flag)
- `/api/poller` — scraper status (last_poll, pushes, cookie health, probe results)
- `/api/mt5/debug` — raw cached data
- `POST /api/cookie` — update cookie via JSON `{"cookie": "..."}`
