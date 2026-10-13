# Bitget Copy Trading Tracker

A self-hosted portfolio dashboard for Bitget MT5 copy trading — tracks your follower account in real time via your session cookie, with a live gold price feed and an iPhone home screen widget.

---

## How it works

```mermaid
flowchart TD
    subgraph YOU ["Your Devices"]
        A["📱 iPhone Widget\n(Scriptable)"]
        B["🖥 Browser Dashboard\n(any device)"]
    end

    subgraph GH ["GitHub Actions (free)"]
        H["🔄 Cookie Refresher\ncron × 2 per day\nverifies + renews session"]
    end

    subgraph RENDER ["Render.com — Free Web Service"]
        C["⚙️ FastAPI Server\nmain.py\n• stores data in memory\n• serves dashboard + API"]
        D["🤖 Browser Poller\nPlaywright + Chromium\n• runs every 30 seconds\n• injects session cookie"]
    end

    subgraph BITGET ["Bitget"]
        E["🔒 Internal Copy Trading APIs\n(cookie auth required)\nbalance · trades · positions"]
        F["📈 Public Market API\n(no auth needed)\nlive XAU/USD price"]
        G["🔑 REST API\n(API key — optional)\nearn balance · deposits"]
    end

    A -->|"GET /api/widget\nevery 5–15 min"| C
    B -->|"GET /api/mt5, /api/prices\netc."| C
    C <-->|"push data\nto server"| D
    D -->|"navigate to bitget.com\ninject cookie → fetch APIs"| E
    E -->|"portfolio balance\nclosed trades\nopen positions"| D
    C -->|"live price\nevery 5 s"| F
    C -->|"earn balance\ndeposit history"| G
    H -->|"1. pull current cookie\n2. verify auth\n3. push renewed cookie"| C
```

**Key points:**
- No Bitget API key required for core tracking — it works via your browser session cookie
- The cookie has a ~5 day TTL; GitHub Actions refreshes it automatically twice a day
- The public price feed (XAU/USD) stays live even when your cookie expires, so open position PnL is always current
- Render's free tier sleeps after 15 min of inactivity — use UptimeRobot to keep it awake

---

## Deploy your own instance

### Step 1 — Fork the repo

1. Go to the GitHub repo page
2. Click **Fork → Create fork**
3. You now have your own copy at `github.com/YOUR_USERNAME/bitget-tracker`

---

### Step 2 — Find your Portfolio IDs

Each copy-trading portfolio on Bitget has a unique ID tied to your account.

1. Log in to [bitget.com](https://bitget.com) in Chrome
2. Go to **Copy Trading → My Copies**
3. Open DevTools (F12) → **Network** tab → filter by **Fetch/XHR**
4. Refresh the page
5. Look for a request named **`getFollowPortfolios`**
6. Click it → **Response** tab → find `portfolioId` under each copy entry

```json
{
  "data": {
    "portfolioDetails": [
      { "portfolioId": "1234567890123456789", "pnl": "312.99" },
      { "portfolioId": "9876543210987654321", "pnl": "-65.90" }
    ]
  }
}
```

Note each `portfolioId` and the trader name you're copying.

---

### Step 3 — Deploy to Render

1. Go to [render.com](https://render.com) and sign up (free)
2. Click **New → Web Service**
3. Connect your GitHub account → select your forked repo
4. Render detects `render.yaml` automatically
5. Under **Environment Variables**, add:

| Variable | Value | Required |
|----------|-------|----------|
| `TRADERS` | `TraderName:portfolioId` | **Yes** |
| `ADMIN_TOKEN` | long random secret | **Strongly recommended** — without it anyone with your URL can replace your cookie, credentials or traders. The dashboard asks for it once per device. |
| `COOKIE_SYNC_TOKEN` | random secret (see Step 5) | No (enables auto-refresh) |
| `POLL_INTERVAL_SEC` | `30` | No (default: 30 s) |
| `BITGET_API_KEY` | your Bitget API key | No (earn/deposits only) |
| `BITGET_API_SECRET` | your Bitget API secret | No |
| `BITGET_API_PASSPHRASE` | your Bitget passphrase | No |

**`TRADERS` format:**
```
# Single CFD trader
TRADERS=TraderName:1234567890123456789

# Multiple traders (comma-separated)
TRADERS=TraderOne:1234567890123456789,TraderTwo:9876543210987654321

# Futures copy trader (add :futures)
TRADERS=FuturesTrader:1234567890123456789:futures
```

6. Click **Deploy** — first build takes ~3 minutes (installs Chromium)
7. Your dashboard is live at `https://YOUR-SERVICE-NAME.onrender.com`

---

### Step 4 — Paste your Bitget session cookie

The tracker needs your Bitget browser session to call its internal APIs. You provide this by copying your cookie from Chrome.

**Option A — Console (fastest):**
1. Log in to [bitget.com](https://bitget.com) in Chrome
2. Open DevTools (F12) → **Console** tab
3. Run: `copy(document.cookie)` ← copies to clipboard instantly

**Option B — Network tab:**
1. Open DevTools → **Network** tab → reload the page
2. Click any request to `bitget.com` → **Headers** → **Request Headers**
3. Find the `cookie:` line → right-click the value → **Copy value**

Then:
4. Open your dashboard → scroll to **Polling Setup**
5. Paste the cookie string → **Save**

The poller starts on the next 30-second cycle. You'll see live data within ~1 minute.

> **Cookie TTL:** `bt_newsessionid` expires after ~5 days. Set up the GitHub Actions auto-refresh below to handle renewal automatically.

---

### Step 5 — Auto-refresh the cookie (GitHub Actions)

Without this step you must manually re-paste the cookie every ~5 days. The GitHub Actions workflow refreshes it automatically twice a day.

> See **[`headless/AUTO-REFRESH.md`](headless/AUTO-REFRESH.md)** for the full setup guide (5 minutes, one-time).

**Quick summary:**

1. Generate a token: `openssl rand -hex 32`
2. **Render** → add env var `COOKIE_SYNC_TOKEN` = that token
3. **GitHub** → repo Settings → Secrets and variables → Actions → add:
   - `TRACKER_URL` = `https://YOUR-SERVICE-NAME.onrender.com`
   - `COOKIE_SYNC_TOKEN` = same token
4. Paste one fresh cookie first (Step 4), then let the Action take over

The workflow runs at 00:00 and 12:00 UTC. If the session is dead and can't be renewed automatically, the Action fails and GitHub emails you — the signal to do a fresh paste.

---

### Step 6 — Keep it awake (UptimeRobot)

Render's free tier puts your service to sleep after 15 minutes with no traffic.

1. Sign up at [uptimerobot.com](https://uptimerobot.com) (free)
2. **New Monitor → HTTP(s)**
3. URL: `https://YOUR-SERVICE-NAME.onrender.com/healthz` (tiny response — saves egress)
4. Interval: **every 5 minutes**

---

### Step 7 — iPhone widget (optional)

1. Install **Scriptable** from the App Store (free)
2. Open Scriptable → tap **+** → paste the entire contents of `scriptable/widget.js`
3. Edit line 1 — set your Render URL:
   ```js
   const SERVER = "https://YOUR-SERVICE-NAME.onrender.com";
   ```
4. Name the script (e.g. "Bitget") → tap **Run** to test
5. Long-press home screen → **+** → search **Scriptable** → choose **Medium** widget
6. Long-press the widget → **Edit Widget** → set Script to "Bitget"

The widget auto-refreshes every 5–15 minutes in the background.

---

## Optional: Bitget API keys (earn + deposit tracking)

Without API keys the tracker still shows all copy trading data. API keys add:
- **Earn balance** (flexible savings — USDT + USDC shown separately)
- **Deposit/withdrawal history** for net investment tracking

To create API keys:
1. Bitget → Avatar → **API Management → Create API**
2. Permissions: **Read Only** — enable Spot + Futures read
3. IP whitelist: `0.0.0.0/0` (open) or your Render outbound IP
4. Add `BITGET_API_KEY`, `BITGET_API_SECRET`, `BITGET_API_PASSPHRASE` to Render env vars

---

## Dashboard reference

| URL | Description |
|-----|-------------|
| `/` | Main dashboard |
| `/api/poller` | Scraper status — `auth_ok`, last scrape, cookie health |
| `/api/mt5` | Portfolio summary (JSON) |
| `/api/mt5/debug` | Raw cached data — useful for diagnosing field names |
| `/api/prices` | Live XAU/USD price (public, no auth) |
| `/api/earn` | Earn balance (requires API key) |

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Dashboard shows "—" everywhere | Check `/api/poller` — if `has_cookie: false`, paste your cookie (Step 4) |
| `auth_ok: false` in poller | Cookie expired — re-paste a fresh one from Chrome DevTools |
| `last_scrape: null` after several polls | Same as above — cookie is dead, re-paste |
| "No open positions" when trades are active | Wait one poll cycle (~30 s); position probe runs at the start of each cycle |
| Stopped trader still showing in cards | Refresh — next poll moves it to Stopped Copies automatically |
| Widget shows "⚠ stale" | Server woke from sleep — pull to refresh; widget catches up next cycle |
| Render build fails | Ensure `Dockerfile` and `render.yaml` are in the root of your fork |
| GitHub Action fails / you get an email | Cookie has expired — re-paste manually (Step 4), then the next Action run will succeed |
