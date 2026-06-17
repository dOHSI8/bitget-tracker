const puppeteer = require('puppeteer-extra');
const StealthPlugin = require('puppeteer-extra-plugin-stealth');
const fs = require('fs');
const path = require('path');

puppeteer.use(StealthPlugin());

const COOKIE_PATH = path.resolve(__dirname, 'data', 'cookies.txt');
const MAX_RETRIES = 3;
const VERIFY_TIMEOUT = 3 * 60 * 1000; // wait up to 3 min per attempt for app approval
const AUTH_COOKIE_NAMES = new Set(['bt_newsessionid', 'bt_sessonid', 'bt_uid']);

/**
 * Try to renew the session headlessly using the existing cookies.
 * If the session is still valid, Bitget's server hands back fresh cookies
 * (extended expiry) without any app approval. Used for proactive re-login.
 *
 * @returns {Promise<{success: boolean, newExpiry?: string, expiryExtended?: boolean}>}
 */
async function tryRefreshSession(options = {}) {
    const { requireExtendedExpiry = false } = options;
    let browser;
    try {
        if (!fs.existsSync(COOKIE_PATH)) {
            console.log('[Login] no cookie file — skip refresh');
            return { success: false };
        }

        const cookies = readLocalCookies();
        const sessionCookie = cookies.find(c => c.name === 'bt_newsessionid');
        if (!sessionCookie) {
            console.log('[Login] no bt_newsessionid — skip refresh');
            return { success: false };
        }

        const nowSec = Date.now() / 1000;
        if (sessionCookie.expires > 0 && sessionCookie.expires < nowSec) {
            console.log('[Login] session cookie already expired — skip refresh');
            return { success: false };
        }

        const oldExpiry = sessionCookie.expires || 0;
        const timeLeftHrs = oldExpiry > 0 ? ((oldExpiry - nowSec) / 3600).toFixed(1) : '?';
        console.log(`[Login] refreshing session (~${timeLeftHrs}h left)...`);

        browser = await puppeteer.launch({
            headless: 'new',
            args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
        });
        const page = await browser.newPage();
        await loadExistingCookies(page);

        await page.goto('https://www.bitget.com', { waitUntil: 'networkidle2', timeout: 30000 });
        await sleep(3000);

        // CRITICAL: verify the session is actually authenticated. Navigating
        // with a dead cookie still leaves cookies in the jar, so "the cookie
        // exists" is NOT proof the session works. Call a real authenticated
        // endpoint and check Bitget's response code/message. Without this we
        // would happily push a dead cookie back to the tracker (false success).
        const authed = await isSessionAuthenticated(page);
        if (!authed) {
            console.log('[Login] session NOT authenticated after navigation — refresh failed, full login required');
            return { success: false, reason: 'not_authenticated' };
        }

        const newCookies = await page.cookies('https://www.bitget.com');
        const newSession = newCookies.find(c => c.name === 'bt_newsessionid');
        if (!newSession || !newSession.value) {
            console.log('[Login] session invalidated by server — full login required');
            return { success: false };
        }

        const newExpiry = newSession.expires || 0;
        await saveCookies(page);

        const expiryStr = newExpiry > 0
            ? new Date(newExpiry * 1000).toISOString()
            : 'unknown';
        console.log(`[Login] session verified authenticated — cookies saved (expires ${expiryStr})`);
        return { success: true, newExpiry: expiryStr, expiryExtended: newExpiry > oldExpiry };

    } catch (err) {
        console.error('[Login] refresh error:', err.message);
        return { success: false };
    } finally {
        if (browser) await browser.close().catch(() => {});
    }
}

/**
 * Auto login with retry. Tries a silent refresh first, then falls back to a
 * full login (phone + password + app approval). Opens a visible browser only
 * when a full login is actually needed.
 *
 * @returns {Promise<boolean>} true on success
 */
async function autoLogin(options = {}) {
    const { requireExtendedRefresh = false } = options;

    const refresh = await tryRefreshSession({ requireExtendedExpiry: requireExtendedRefresh });
    if (refresh.success) {
        console.log('[Login] renewed cookies without a full login');
        return true;
    }
    console.log('[Login] refresh unavailable — full login required...');

    const phone = process.env.BITGET_PHONE;
    const password = process.env.BITGET_PASSWORD;

    if (!phone || !password) {
        console.log('[Login] BITGET_PHONE / BITGET_PASSWORD not set');
        console.log('[Login] opening browser for manual login...');
        return await manualLogin();
    }

    for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
        console.log(`\n[Login] auto login — attempt ${attempt}/${MAX_RETRIES}`);

        const result = await attemptLogin(phone, password, {
            includeAuthCookies: !requireExtendedRefresh,
        });

        if (result === 'success') {
            return true;
        } else if (result === 'verify_timeout') {
            console.log(`[Login] no approval within ${VERIFY_TIMEOUT / 60000} min — retrying...`);
        } else {
            console.error('[Login] auto login failed (page may have changed) → manual login...');
            return await manualLogin();
        }
    }

    console.error(`[Login] failed after ${MAX_RETRIES} attempts`);
    return false;
}

/**
 * One full login attempt.
 * @returns {'success' | 'verify_timeout' | 'error'}
 */
async function attemptLogin(phone, password, options = {}) {
    const { includeAuthCookies = true } = options;
    let browser;
    try {
        browser = await puppeteer.launch({ headless: false, defaultViewport: null });
        const page = await browser.newPage();

        // Load prior cookies (fingerprint, terminalCode...) so Bitget remembers
        // this device and skips the "new device" warning.
        await loadExistingCookies(page, { includeAuthCookies });

        await page.goto('https://www.bitget.com/login', { waitUntil: 'networkidle2', timeout: 60000 });
        await sleep(2000);

        await dismissCookieConsent(page);
        await fillUsernameAndNext(page, phone);
        await fillPasswordAndNext(page, password);
        await handleDialogs(page);

        console.log('');
        console.log('=============================================');
        console.log('  Entered phone + password + Next/Continue');
        console.log('  Waiting for verification...');
        console.log('  Approve the login in your Bitget app');
        console.log(`  Timeout: ${VERIFY_TIMEOUT / 60000} min`);
        console.log('=============================================');
        console.log('');

        const success = await waitForLoginSuccess(page, VERIFY_TIMEOUT);
        if (success) {
            await saveCookies(page);
            return 'success';
        }
        return 'verify_timeout';
    } catch (err) {
        console.error('[Login] error:', err.message);
        return 'error';
    } finally {
        if (browser) await browser.close().catch(() => {});
    }
}

// ============================
// Login Steps
// ============================

async function fillUsernameAndNext(page, phone) {
    await page.waitForSelector('input[name="username"]', { timeout: 10000 });
    await sleep(500);
    const usernameInput = await page.$('input[name="username"]');
    if (!usernameInput) throw new Error('username field not found');
    await usernameInput.click({ clickCount: 3 });
    await usernameInput.type(phone, { delay: 30 });
    console.log('[Login] step 1: entered phone/email');
    await sleep(300);
    await clickButton(page, 'next', 'Step 1');
}

async function fillPasswordAndNext(page, password) {
    await page.waitForSelector('input[type="password"]', { timeout: 10000 });
    await sleep(500);
    const passwordInput = await page.$('input[type="password"]');
    if (!passwordInput) throw new Error('password field not found');
    await passwordInput.click();
    await passwordInput.type(password, { delay: 30 });
    console.log('[Login] step 2: entered password');
    await sleep(300);
    await clickButton(page, 'next', 'Step 2');
}

/**
 * Dismiss post-login dialogs (Safety reminder, Cross-device verification) by
 * clicking any visible "Continue" button. Loops since several may stack.
 */
async function handleDialogs(page) {
    let dialogCount = 0;
    const maxDialogs = 5;
    while (dialogCount < maxDialogs) {
        await sleep(2000);
        const clicked = await page.evaluate(() => {
            const btns = [...document.querySelectorAll('button')];
            const continueBtn = btns.find(b => {
                const text = b.textContent.trim().toLowerCase();
                const rect = b.getBoundingClientRect();
                return text === 'continue' && rect.width > 0 && rect.height > 0;
            });
            if (continueBtn) { continueBtn.click(); return true; }
            return false;
        });
        if (clicked) {
            dialogCount++;
            console.log(`[Login] clicked Continue (dialog #${dialogCount})`);
            await sleep(1000);
        } else {
            break;
        }
    }
    if (dialogCount > 0) console.log(`[Login] handled ${dialogCount} dialog(s)`);
}

async function dismissCookieConsent(page) {
    try {
        const clicked = await page.evaluate(() => {
            const btns = [...document.querySelectorAll('button')];
            const btn = btns.find(b => b.textContent.trim().toLowerCase().includes('accept all'));
            if (btn) { btn.click(); return true; }
            return false;
        });
        if (clicked) {
            console.log('[Login] dismissed cookie consent');
            await sleep(1000);
        }
    } catch { /* no consent popup */ }
}

/** Find and click a button by text, waiting until it is enabled. */
async function clickButton(page, buttonText, stepLabel) {
    const lowerText = buttonText.toLowerCase();
    for (let i = 0; i < 20; i++) {
        const state = await page.evaluate((text) => {
            const btns = [...document.querySelectorAll('button')];
            const btn = btns.find(b => b.textContent.trim().toLowerCase() === text);
            if (!btn) return 'not_found';
            if (btn.disabled) return 'disabled';
            return 'ready';
        }, lowerText);
        if (state === 'ready') break;
        if (state === 'not_found') {
            console.warn(`[Login] ${stepLabel}: button "${buttonText}" not found`);
            return;
        }
        await sleep(500);
    }
    const clicked = await page.evaluate((text) => {
        const btns = [...document.querySelectorAll('button')];
        const btn = btns.find(b => b.textContent.trim().toLowerCase() === text && !b.disabled);
        if (btn) { btn.click(); return true; }
        return false;
    }, lowerText);
    if (clicked) {
        console.log(`[Login] ${stepLabel}: clicked "${buttonText}"`);
        await sleep(2000);
    } else {
        console.warn(`[Login] ${stepLabel}: could not click "${buttonText}"`);
    }
}

// ============================
// Cookie helpers
// ============================

function sleep(ms) {
    return new Promise(r => setTimeout(r, ms));
}

/**
 * Ask Bitget whether the current cookies actually authenticate, by calling a
 * known authenticated endpoint from within the page (credentials: 'include').
 * Returns false on the "Log in expired" sentinel (code 00004 / message), on an
 * HTML/Cloudflare redirect, or on any error. This is the honest check that
 * "the cookie exists" cannot give us.
 */
async function isSessionAuthenticated(page) {
    try {
        return await page.evaluate(async () => {
            try {
                const r = await fetch('/v1/trace/mt5/trace/getFollowPortfolios', {
                    method: 'POST', credentials: 'include',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({}),
                });
                const text = await r.text();
                if (text.trimStart().startsWith('<')) return false; // CF / login redirect
                const j = JSON.parse(text);
                const msg = (j && j.msg ? String(j.msg) : '').toLowerCase();
                if (msg.includes('expired') || msg.includes('re-log') || msg.includes('log in')) return false;
                return ['00000', '200', '0'].includes(String(j && j.code));
            } catch { return false; }
        });
    } catch {
        return false;
    }
}

/** Read data/cookies.txt (base64-encoded JSON, or raw JSON) → cookie array. */
function readLocalCookies() {
    const fileContent = fs.readFileSync(COOKIE_PATH, 'utf-8').trim();
    try {
        return JSON.parse(Buffer.from(fileContent, 'base64').toString('utf-8'));
    } catch {
        return JSON.parse(fileContent);
    }
}

/**
 * Load existing cookies into the page so Bitget remembers this device.
 * Optionally skip the auth cookies (forces a clean re-auth).
 */
async function loadExistingCookies(page, options = {}) {
    const { includeAuthCookies = true } = options;
    try {
        if (!fs.existsSync(COOKIE_PATH)) return;
        const cookies = readLocalCookies();
        if (Array.isArray(cookies) && cookies.length > 0) {
            const cookiesToLoad = includeAuthCookies
                ? cookies
                : cookies.filter(c => !AUTH_COOKIE_NAMES.has(c.name));
            if (cookiesToLoad.length > 0) await page.setCookie(...cookiesToLoad);
            console.log(`[Login] loaded ${cookiesToLoad.length}/${cookies.length} cookies`);
        }
    } catch (err) {
        console.warn('[Login] could not load prior cookies:', err.message);
    }
}

/** Wait until the URL leaves the login page (= logged in). */
async function waitForLoginSuccess(page, timeout) {
    const start = Date.now();
    while (Date.now() - start < timeout) {
        try {
            const url = page.url();
            if (url.includes('bitget.com') && !url.includes('/login') && !url.includes('/signin')) {
                await sleep(3000);
                return true;
            }
        } catch {
            return false;
        }
        await sleep(1000);
    }
    return false;
}

/** Save all cookies to data/cookies.txt (base64-encoded JSON). */
async function saveCookies(page) {
    const cookies = await page.cookies();
    const base64Str = Buffer.from(JSON.stringify(cookies)).toString('base64');
    const dataDir = path.dirname(COOKIE_PATH);
    if (!fs.existsSync(dataDir)) fs.mkdirSync(dataDir, { recursive: true });
    fs.writeFileSync(COOKIE_PATH, base64Str);
    console.log(`[Login] saved ${cookies.length} cookies`);
}

/** Manual fallback: open a browser and let the user log in by hand. */
async function manualLogin() {
    let browser;
    try {
        browser = await puppeteer.launch({ headless: false, defaultViewport: null });
        const page = await browser.newPage();
        await page.goto('https://www.bitget.com/login', { waitUntil: 'networkidle2', timeout: 60000 });
        console.log('');
        console.log('=============================================');
        console.log('  Log in to Bitget in the browser.');
        console.log('  Cookies save automatically on success.');
        console.log('  Timeout: 5 min');
        console.log('=============================================');
        console.log('');
        const success = await waitForLoginSuccess(page, 5 * 60 * 1000);
        if (success) {
            await saveCookies(page);
            return true;
        }
        console.error('[Login] timeout');
        return false;
    } catch (err) {
        console.error('[Login] error:', err.message);
        return false;
    } finally {
        if (browser) await browser.close().catch(() => {});
    }
}

// --- CLI: npm run login ---
if (require.main === module) {
    try {
        require('dotenv').config({ path: path.resolve(__dirname, '.env') });
    } catch { /* optional */ }
    autoLogin().then((success) => {
        console.log(success ? '\n✅ Cookies ready' : '\n❌ Login failed');
        process.exit(success ? 0 : 1);
    });
}

module.exports = { autoLogin, tryRefreshSession, COOKIE_PATH, readLocalCookies };
