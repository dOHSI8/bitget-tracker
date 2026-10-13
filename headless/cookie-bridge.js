const fs = require('fs');
const path = require('path');
const { COOKIE_PATH } = require('./login');

/**
 * Convert a Puppeteer cookie array → a `document.cookie`-style string
 * (`name=value; name=value`). This is exactly the format the tracker's
 * /api/poller/cookie endpoint expects (same as pasting document.cookie).
 */
function cookiesToString(cookies) {
    return cookies
        .filter(c => c && c.name)
        .map(c => `${c.name}=${c.value}`)
        .join('; ');
}

/**
 * Convert a `document.cookie` string → a Puppeteer cookie array suitable for
 * page.setCookie(). Expiry/domain metadata is lost on this hop, but the server
 * re-issues full cookies (with expiry) on the next page load.
 */
function stringToCookies(cookieStr) {
    return cookieStr
        .split('; ')
        .filter(pair => pair.includes('='))
        .map(pair => {
            const i = pair.indexOf('=');
            return {
                name: pair.slice(0, i).trim(),
                value: pair.slice(i + 1),
                domain: '.bitget.com',
                path: '/',
            };
        })
        .filter(c => c.name);
}

/** Pull the current cookie string from the tracker (token-protected export). */
async function pullCookieFromTracker(trackerUrl, token) {
    const res = await fetch(`${trackerUrl}/api/poller/cookie/export`, {
        headers: { 'X-Sync-Token': token },
    });
    if (!res.ok) {
        throw new Error(`export failed: HTTP ${res.status}`);
    }
    const body = await res.json();
    if (!body.ok || !body.cookie) {
        throw new Error(`export returned no cookie: ${body.error || 'unknown'}`);
    }
    return body.cookie;
}

/** Push a cookie string to the tracker. Sends COOKIE_SYNC_TOKEN (or
 *  ADMIN_TOKEN) when set — required once the tracker has ADMIN_TOKEN. */
async function pushCookieToTracker(trackerUrl, cookieStr) {
    const headers = { 'Content-Type': 'application/json' };
    const token = process.env.COOKIE_SYNC_TOKEN || process.env.ADMIN_TOKEN || '';
    if (token) headers['X-Admin-Token'] = token;
    const res = await fetch(`${trackerUrl}/api/poller/cookie`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ cookie: cookieStr }),
    });
    if (!res.ok) {
        throw new Error(`push failed: HTTP ${res.status}`);
    }
    const body = await res.json();
    if (!body.ok) {
        throw new Error(`push rejected: ${body.error || 'unknown'}`);
    }
    return body.length;
}

/** Write a Puppeteer cookie array to data/cookies.txt (base64 JSON). */
function writeLocalCookies(cookies) {
    const dataDir = path.dirname(COOKIE_PATH);
    if (!fs.existsSync(dataDir)) fs.mkdirSync(dataDir, { recursive: true });
    fs.writeFileSync(COOKIE_PATH, Buffer.from(JSON.stringify(cookies)).toString('base64'));
}

module.exports = {
    cookiesToString,
    stringToCookies,
    pullCookieFromTracker,
    pushCookieToTracker,
    writeLocalCookies,
};
