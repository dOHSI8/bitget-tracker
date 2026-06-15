require('dotenv').config();
const puppeteer = require('puppeteer');
const path = require('path');

const TRACKER_URL = process.env.TRACKER_URL || 'https://YOUR-SERVICE-NAME.onrender.com';
const BITGET_PAGE = process.env.BITGET_PAGE || 'https://www.bitget.com/copy-trading/mt5/follower/detail?portfolioId=YOUR_PORTFOLIO_ID';
const PORTFOLIO_ID = process.env.PORTFOLIO_ID || 'YOUR_PORTFOLIO_ID';
const POLL_INTERVAL = parseInt(process.env.POLL_INTERVAL_MS) || 60_000;
const SCRAPE_INTERVAL = parseInt(process.env.SCRAPE_INTERVAL_MS) || 30_000;
const REFRESH_INTERVAL = parseInt(process.env.REFRESH_INTERVAL_MS) || 3_600_000;
const USER_DATA_DIR = path.join(__dirname, 'browser-data');

function log(...args) {
  console.log(`[${new Date().toISOString()}]`, ...args);
}

async function pushToTracker(kind, data) {
  try {
    const res = await fetch(TRACKER_URL + '/api/push/mt5', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind, data }),
    });
    if (!res.ok) log('push failed:', res.status);
  } catch (e) {
    log('push error:', e.message);
  }
}

function classifyAndPush(url, data) {
  if (url.includes('/tracePosition') || url.includes('/trace_position')) {
    log('captured positions');
    pushToTracker('positions', data);
    return;
  }
  if (url.includes('/positionHistory') || url.includes('/position_history')) {
    log('captured history');
    pushToTracker('history', data);
    return;
  }
  if (url.includes('/balanceHistory') || url.includes('/balance_history')
      || url.includes('/balanceLog') || url.includes('/balance_log')
      || url.includes('/fundFlow') || url.includes('/fund_flow')) {
    log('captured balance_history');
    pushToTracker('balance_history', data.data || data);
    return;
  }
  if (url.includes('/traceDetail') || url.includes('/trace_detail')
      || url.includes('/copyDetail') || url.includes('/copy_detail')
      || url.includes('/accountInfo') || url.includes('/account_info')) {
    const d = data.data || data;
    if (d && (d.totalBalance || d.totalEquity || d.balance)) {
      log('captured copy_details');
      pushToTracker('copy_details', d);
    }
    return;
  }
  if (typeof data === 'object' && data !== null) {
    const d = data.data || data;
    if (d && typeof d === 'object' && !Array.isArray(d)) {
      const keys = Object.keys(d);
      const balKey = keys.find(k => /balance|equity|totalBal|totalEquity/i.test(k));
      if (balKey) {
        log('sniffed balance from', url, 'key=', balKey);
        pushToTracker('copy_details', d);
        return;
      }
    }
    const rows = (d && d.rows) || (d && d.list) || (Array.isArray(d) ? d : null);
    if (rows && rows.length > 0 && typeof rows[0] === 'object') {
      const sample = rows[0];
      const typ = sample.type || sample.typeName || '';
      if (/add|transfer|deposit|withdraw/i.test(typ)) {
        log('sniffed balance_history from', url);
        pushToTracker('balance_history', rows);
        return;
      }
    }
  }
  pushToTracker('balance_sniff', { url, data });
}

async function setupResponseInterception(page) {
  page.on('response', async (response) => {
    const url = response.url();
    if (!url.includes('/v1/')) return;
    try {
      const json = await response.json();
      classifyAndPush(url, json);
    } catch (_) {}
  });
}

async function activePoll(page) {
  log('polling...');
  try {
    const posResult = await page.evaluate(async (portfolioId) => {
      const r = await fetch('/v1/trace/mt5/data/tracePosition', {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ portfolioId }),
      });
      return r.ok ? await r.json() : null;
    }, PORTFOLIO_ID);
    if (posResult) await pushToTracker('positions', posResult);
  } catch (e) { log('positions poll error:', e.message); }

  try {
    const histResult = await page.evaluate(async (portfolioId) => {
      const r = await fetch('/v1/trace/mt5/trace/positionHistory', {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ portfolioId, pageNo: 1, pageSize: 50 }),
      });
      return r.ok ? await r.json() : null;
    }, PORTFOLIO_ID);
    if (histResult) await pushToTracker('history', histResult);
  } catch (e) { log('history poll error:', e.message); }

  const balEndpoints = [
    '/v1/trace/mt5/trace/balanceHistory',
    '/v1/trace/mt5/data/balanceHistory',
    '/v1/trace/mt5/trace/fundFlow',
  ];
  for (const ep of balEndpoints) {
    try {
      const balResult = await page.evaluate(async (endpoint, portfolioId) => {
        const r = await fetch(endpoint, {
          method: 'POST', credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ portfolioId, pageNo: 1, pageSize: 100 }),
        });
        if (!r.ok) return null;
        const json = await r.json();
        const rows = json?.data?.rows || json?.data?.list || json?.data || [];
        return Array.isArray(rows) && rows.length > 0 ? rows : null;
      }, ep, PORTFOLIO_ID);
      if (balResult) {
        log('polled balance_history from', ep);
        await pushToTracker('balance_history', balResult);
        break;
      }
    } catch (_) {}
  }
}

async function scrapeCopyDetails(page) {
  try {
    const details = await page.evaluate(() => {
      const text = document.body?.innerText || '';

      const balMatch = text.match(/Total\s*balance\s*\(?USDT\)?\s*[\n\r]*\s*([\d,]+\.?\d*)/i);
      const eqMatch = text.match(/Total\s*equity\s*\(?USDT\)?\s*[\n\r]*\s*([\d,]+\.?\d*)/i);
      const bal = balMatch ? parseFloat(balMatch[1].replace(/,/g, '')) : 0;
      const eq = eqMatch ? parseFloat(eqMatch[1].replace(/,/g, '')) : 0;
      const value = bal || eq;

      const netMatch = text.match(/Est\.?\s*net\s*profit\s*\(?USDT\)?\s*[\n\r]*\s*[+\-]?([\d,]+\.?\d*)/i);
      const realMatch = text.match(/(?<![Uu]n)(?:^|[^a-zA-Z])[Rr]ealized\s*PnL\s*\(?USDT\)?\s*[\n\r]*\s*[+\-]?([\d,]+\.?\d*)/);
      const unrealMatch = text.match(/Unrealized\s*PnL\s*\(?USDT\)?\s*[\n\r]*\s*[+\-]?([\d,]+\.?\d*)/i);

      const netProfit = netMatch ? parseFloat(netMatch[1].replace(/,/g, '')) : null;
      const realPnl = realMatch ? parseFloat(realMatch[1].replace(/,/g, '')) : null;
      const unrealPnl = unrealMatch ? parseFloat(unrealMatch[1].replace(/,/g, '')) : null;

      const netSign = netMatch && text.match(/Est\.?\s*net\s*profit\s*\(?USDT\)?\s*[\n\r]*\s*-/) ? -1 : 1;
      const realSign = realMatch && text.match(/(?<![Uu]n)(?:^|[^a-zA-Z])[Rr]ealized\s*PnL\s*\(?USDT\)?\s*[\n\r]*\s*-/) ? -1 : 1;

      if (value <= 0 && netProfit === null) return null;

      const result = { totalBalance: value, totalEquity: eq || value };
      if (netProfit !== null) result.estNetProfit = netProfit * netSign;
      if (realPnl !== null) result.realizedPnl = realPnl * realSign;
      if (unrealPnl !== null) result.unrealizedPnl = unrealPnl;
      return result;
    });

    if (details) {
      log('DOM scraped:', JSON.stringify(details));
      await pushToTracker('copy_details', details);
    }
  } catch (e) { log('scrape copy_details error:', e.message); }

  try {
    const historyRows = await page.evaluate(() => {
      const text = document.body?.innerText || '';
      const rows = [];
      const addMatches = text.matchAll(/\bAdd\b[\s\S]{0,30}?([\d,]+\.?\d+)\s*USDT/gi);
      for (const m of addMatches) {
        rows.push({ type: 'Add', amount: parseFloat(m[1].replace(/,/g, '')) });
      }
      const outMatches = text.matchAll(/Transfer\s*out[\s\S]{0,30}?([\d,]+\.?\d+)\s*USDT/gi);
      for (const m of outMatches) {
        rows.push({ type: 'Transfer out', amount: parseFloat(m[1].replace(/,/g, '')) });
      }
      return rows;
    });

    if (historyRows.length > 0) {
      log('DOM scraped balance_history:', historyRows.length, 'rows');
      await pushToTracker('balance_history', historyRows);
    }
  } catch (e) { log('scrape balance_history error:', e.message); }
}

async function clickTab(page, tabName) {
  try {
    const clicked = await page.evaluate((name) => {
      const els = document.querySelectorAll('[role="tab"], [class*="tab"], [class*="Tab"], button, span, div');
      for (const el of els) {
        const text = (el.innerText || '').trim();
        if (text === name || text.toLowerCase() === name.toLowerCase()) {
          el.click();
          return true;
        }
      }
      return false;
    }, tabName);
    if (clicked) log('clicked tab:', tabName);
    return clicked;
  } catch (e) {
    log('clickTab error:', e.message);
    return false;
  }
}

async function autoTabCycle(page) {
  await new Promise(r => setTimeout(r, 10_000));
  await clickTab(page, 'Balance history');
  await new Promise(r => setTimeout(r, 5_000));
  await scrapeCopyDetails(page);
  await new Promise(r => setTimeout(r, 5_000));
  await clickTab(page, 'Positions');
}

async function main() {
  log('Starting headless scraper...');
  log('Tracker URL:', TRACKER_URL);
  log('Bitget page:', BITGET_PAGE);

  const browser = await puppeteer.launch({
    headless: 'new',
    userDataDir: USER_DATA_DIR,
    args: [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-dev-shm-usage',
      '--disable-gpu',
      '--no-first-run',
      '--no-zygote',
      '--single-process',
      '--disable-extensions',
      '--disable-background-networking',
      '--disable-default-apps',
      '--disable-sync',
      '--disable-translate',
      '--metrics-recording-only',
      '--mute-audio',
      '--no-default-browser-check',
      '--disable-hang-monitor',
      '--disable-prompt-on-repost',
      '--disable-client-side-phishing-detection',
      '--disable-component-update',
      '--disable-domain-reliability',
      '--js-flags=--max-old-space-size=256',
    ],
  });

  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 900 });
  await page.setUserAgent(
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
  );

  await setupResponseInterception(page);

  log('Navigating to Bitget...');
  await page.goto(BITGET_PAGE, { waitUntil: 'networkidle2', timeout: 60_000 });
  log('Page loaded');

  const title = await page.title();
  log('Page title:', title);

  // Check if logged in
  const pageText = await page.evaluate(() => document.body?.innerText?.slice(0, 500) || '');
  if (pageText.includes('Log In') || pageText.includes('Sign Up')) {
    log('WARNING: Not logged in! Run "npm run login" first to save your session.');
    await browser.close();
    process.exit(1);
  }

  // Initial tab cycle
  autoTabCycle(page);

  // Initial poll
  await activePoll(page);

  // Poll every 60s
  setInterval(() => activePoll(page), POLL_INTERVAL);

  // Scrape DOM every 30s
  setTimeout(() => scrapeCopyDetails(page), 5_000);
  setInterval(() => scrapeCopyDetails(page), SCRAPE_INTERVAL);

  // Auto-refresh every 1 hour
  setInterval(async () => {
    log('Auto-refreshing page...');
    try {
      await page.goto(BITGET_PAGE, { waitUntil: 'networkidle2', timeout: 60_000 });
      autoTabCycle(page);
    } catch (e) {
      log('Refresh error:', e.message);
    }
  }, REFRESH_INTERVAL);

  log('Scraper running. Press Ctrl+C to stop.');

  process.on('SIGINT', async () => {
    log('Shutting down...');
    await browser.close();
    process.exit(0);
  });

  process.on('SIGTERM', async () => {
    log('Shutting down...');
    await browser.close();
    process.exit(0);
  });
}

main().catch(e => {
  console.error('Fatal error:', e);
  process.exit(1);
});
