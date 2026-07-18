# Recovering the Bitget cookie (when polling stops)

The tracker authenticates to Bitget with a **session cookie**, not an API key.
That session has a short life (~5 days) and Bitget periodically forces a
**phone-approved re-login** that no automation can complete for you. When that
happens you'll see the *Refresh Bitget cookie* GitHub Action fail and email you.

Re-seeding a fresh cookie takes about **30 seconds**. Use Method A.

> **Why this can't be fully automated:** the poller self-refreshes the cookie on
> every cycle (`browser_poller.py:_persist_refreshed_cookies`) and the hourly-ish
> GitHub Action extends it as a backup — but both only *extend a session that is
> still alive*. Once Bitget expires it, only a real login (with the app-approval
> tap) can mint a new one. On top of that, Render's free tier has an ephemeral
> disk, so a restart reverts to the `BITGET_COOKIE` env-var snapshot until the
> next refresh. Manual re-seeding is the reliable reset.

---

## Method A — Bookmarklet + dashboard paste (fastest)

**One-time setup:** create a new browser bookmark, name it `Copy Bitget cookie`,
and paste this as the **URL**:

```
javascript:(async()=>{try{await navigator.clipboard.writeText(document.cookie);alert('✅ Bitget cookie copied.\n\nNow: tracker dashboard → Polling Setup → paste → Save.');}catch(e){prompt('Copy this cookie manually:',document.cookie);}})()
```

**Each time polling dies:**

1. Open **https://www.bitget.com** in a tab where you're **logged in**.
2. Click the **Copy Bitget cookie** bookmark. The cookie is now on your clipboard.
3. Open your tracker dashboard (`https://bitget-tracker-v2.onrender.com`) →
   **Polling Setup** → paste into the cookie box → **Save**.
4. Done. The next poll (within ~10 min) picks it up. To confirm sooner, check
   `/api/poller` — `auth_ok` should flip to `true`.

---

## Method B — DevTools console (no bookmarklet)

1. On **bitget.com** (logged in): open DevTools → **Console**.
2. Run `copy(document.cookie)` (copies straight to clipboard) — or run
   `document.cookie` and copy the printed string.
3. Paste into the dashboard → **Polling Setup** → **Save** (as above).

---

## Method C — Full fresh login from your PC (`headless/`)

Use this when the cookie is so dead that Methods A/B don't stick (i.e. Bitget
wants a brand-new app-approved session).

```bash
cd headless
npm install
cp .env.example .env        # then edit .env:
                            #   TRACKER_URL=https://bitget-tracker-v2.onrender.com
npm run login               # opens a real browser — log in + approve on your phone
node push-cookie.js         # uploads the fresh cookie to the tracker
```

`npm run login` saves the session to `headless/data/cookies.txt`; `push-cookie.js`
POSTs it to the tracker's `/api/poller/cookie` (same endpoint the dashboard uses).

---

## Verifying it worked

- Dashboard shows live balance / positions again, **or**
- `GET /api/poller` returns `"auth_ok": true` with a recent `last_poll`, **or**
- Re-run the **Refresh Bitget cookie** workflow (Actions → Run workflow) — it
  should now go **green**.

## Re-arming the alert

Nothing to do — the workflow keeps running every 6h and will email you again the
next time the session dies. If the emails get noisy, widen the cron in
`.github/workflows/refresh-cookie.yml` (e.g. `17 */12 * * *` for twice a day).
