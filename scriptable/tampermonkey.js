// ==UserScript==
// @name         Bitget CFD → Tracker
// @namespace    bitget-tracker
// @version      2.0
// @description  Relays Bitget CFD copy trading data to your self-hosted tracker
// @author       you
// @match        https://www.bitget.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @connect      localhost
// @connect      *
// @run-at       document-start
// ==/UserScript==

(function () {
  'use strict';

  // ── CONFIG ─────────────────────────────────────────────────────────────────
  const TRACKER_URL = 'https://bitget-tracker.onrender.com';

  // ── Push to tracker ────────────────────────────────────────────────────────
  function pushToTracker(kind, data) {
    GM_xmlhttpRequest({
      method: 'POST',
      url: TRACKER_URL + '/api/push/mt5',
      headers: { 'Content-Type': 'application/json' },
      data: JSON.stringify({ kind, data }),
      onerror: (e) => console.warn('[Bitget Tracker] push failed', e),
    });
  }

  // ── Detect data type from URL and response ────────────────────────────────
  function classifyAndPush(url, data) {
    // Open positions
    if (url.includes('/tracePosition') || url.includes('/trace_position')) {
      console.log('[Bitget Tracker] captured positions');
      pushToTracker('positions', data);
      return;
    }

    // Trade history (closed positions)
    if (url.includes('/positionHistory') || url.includes('/position_history')) {
      console.log('[Bitget Tracker] captured history');
      pushToTracker('history', data);
      return;
    }

    // Balance history (Add / Transfer out records)
    if (url.includes('/balanceHistory') || url.includes('/balance_history')
        || url.includes('/balanceLog') || url.includes('/balance_log')
        || url.includes('/fundFlow') || url.includes('/fund_flow')) {
      console.log('[Bitget Tracker] captured balance_history');
      pushToTracker('balance_history', data.data || data);
      return;
    }

    // Copy details page (total balance, equity, etc.)
    if (url.includes('/traceDetail') || url.includes('/trace_detail')
        || url.includes('/copyDetail') || url.includes('/copy_detail')
        || url.includes('/accountInfo') || url.includes('/account_info')) {
      const d = data.data || data;
      if (d && (d.totalBalance || d.totalEquity || d.balance)) {
        console.log('[Bitget Tracker] captured copy_details');
        pushToTracker('copy_details', d);
      }
      return;
    }

    // Broad sniff: any response with balance-like fields
    if (typeof data === 'object' && data !== null) {
      const d = data.data || data;
      // Check nested object for any balance/equity field
      if (d && typeof d === 'object' && !Array.isArray(d)) {
        const keys = Object.keys(d);
        const balKey = keys.find(k => /balance|equity|totalBal|totalEquity/i.test(k));
        if (balKey) {
          console.log('[Bitget Tracker] sniffed balance from', url, 'key=', balKey);
          pushToTracker('copy_details', d);
          return;
        }
      }
      // Broad sniff: array with Add/Transfer out entries = balance history
      const rows = (d && d.rows) || (d && d.list) || (Array.isArray(d) ? d : null);
      if (rows && rows.length > 0 && typeof rows[0] === 'object') {
        const sample = rows[0];
        const typ = sample.type || sample.typeName || '';
        if (/add|transfer|deposit|withdraw/i.test(typ)) {
          console.log('[Bitget Tracker] sniffed balance_history from', url);
          pushToTracker('balance_history', rows);
          return;
        }
      }
    }

    // Debug: send ALL unclassified /v1/ responses so we can find the right fields
    const snippet = JSON.stringify(data).slice(0, 500);
    console.log('[Bitget Tracker] UNCLASSIFIED:', url, snippet);
    pushToTracker('balance_sniff', { url, data });
  }

  // ── Intercept fetch ────────────────────────────────────────────────────────
  const _origFetch = window.fetch;
  window.fetch = function (...args) {
    const url = typeof args[0] === 'string' ? args[0] : (args[0]?.url || '');
    const promise = _origFetch.apply(this, args);

    if (url.includes('/v1/')) {
      promise.then(r => r.clone().json()).then(data => {
        classifyAndPush(url, data);
      }).catch(() => {});
    }

    return promise;
  };

  // ── Intercept XMLHttpRequest ──────────────────────────────────────────────
  const _origOpen = XMLHttpRequest.prototype.open;
  const _origSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this._trackerUrl = url;
    return _origOpen.call(this, method, url, ...rest);
  };

  XMLHttpRequest.prototype.send = function (...args) {
    const url = this._trackerUrl || '';
    if (url.includes('/v1/')) {
      this.addEventListener('load', () => {
        try {
          const data = JSON.parse(this.responseText);
          classifyAndPush(url, data);
        } catch (_) {}
      });
    }
    return _origSend.apply(this, args);
  };

  // ── Extract portfolioId from the current page URL ─────────────────────────
  function getPortfolioId() {
    const pathMatch = location.pathname.match(/\/(\d{15,})/);
    if (pathMatch) return pathMatch[1];
    const urlParams = new URLSearchParams(location.search);
    return urlParams.get('portfolioId') || GM_getValue('portfolio_id', '');
  }

  // ── Active poll (calls endpoints directly every 60s) ──────────────────────
  async function activePoll() {
    const portfolioId = getPortfolioId();
    if (portfolioId) GM_setValue('portfolio_id', portfolioId);
    console.log('[Bitget Tracker] polling, portfolioId=', portfolioId);

    // Positions
    try {
      const r = await fetch('/v1/trace/mt5/data/tracePosition', {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ portfolioId }),
      });
      if (r.ok) { pushToTracker('positions', await r.json()); }
    } catch (e) { console.warn('[Bitget Tracker] positions error:', e); }

    // Trade history
    try {
      const r = await fetch('/v1/trace/mt5/trace/positionHistory', {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ portfolioId, pageNo: 1, pageSize: 50 }),
      });
      if (r.ok) { pushToTracker('history', await r.json()); }
    } catch (e) { console.warn('[Bitget Tracker] history error:', e); }

    // Balance history (try common endpoint patterns)
    for (const ep of [
      '/v1/trace/mt5/trace/balanceHistory',
      '/v1/trace/mt5/data/balanceHistory',
      '/v1/trace/mt5/trace/fundFlow',
    ]) {
      try {
        const r = await fetch(ep, {
          method: 'POST', credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ portfolioId, pageNo: 1, pageSize: 100 }),
        });
        if (r.ok) {
          const json = await r.json();
          const rows = json?.data?.rows || json?.data?.list || json?.data || [];
          if (Array.isArray(rows) && rows.length > 0) {
            console.log('[Bitget Tracker] polled balance_history from', ep);
            pushToTracker('balance_history', rows);
            break;
          }
        }
      } catch (_) {}
    }
  }

  // ── DOM scraper: read balance & equity directly from the page ────────────
  function scrapeCopyDetails() {
    const text = document.body?.innerText || '';

    // Look for "Total balance" or "Total equity" followed by a number
    const balMatch = text.match(/Total\s*balance\s*\(?USDT\)?\s*[\n\r]*\s*([\d,]+\.?\d*)/i);
    const eqMatch = text.match(/Total\s*equity\s*\(?USDT\)?\s*[\n\r]*\s*([\d,]+\.?\d*)/i);

    const bal = balMatch ? parseFloat(balMatch[1].replace(/,/g, '')) : 0;
    const eq = eqMatch ? parseFloat(eqMatch[1].replace(/,/g, '')) : 0;
    const value = bal || eq;

    if (value > 0) {
      console.log('[Bitget Tracker] DOM scraped balance:', value);
      pushToTracker('copy_details', { totalBalance: value, totalEquity: eq || value });
    }

    // Scrape balance history from full page text
    const historyRows = [];
    // Match "Add" entries: look for "Add" followed by a number and "USDT"
    const addMatches = text.matchAll(/\bAdd\b[\s\S]{0,30}?([\d,]+\.?\d+)\s*USDT/gi);
    for (const m of addMatches) {
      historyRows.push({ type: 'Add', amount: parseFloat(m[1].replace(/,/g, '')) });
    }
    // Match "Transfer out" entries
    const outMatches = text.matchAll(/Transfer\s*out[\s\S]{0,30}?([\d,]+\.?\d+)\s*USDT/gi);
    for (const m of outMatches) {
      historyRows.push({ type: 'Transfer out', amount: parseFloat(m[1].replace(/,/g, '')) });
    }
    if (historyRows.length > 0) {
      console.log('[Bitget Tracker] DOM scraped balance_history:', historyRows.length, 'rows');
      pushToTracker('balance_history', historyRows);
    } else {
      // Debug: send a snippet of the page text so we can see what's there
      const snippet = text.slice(0, 2000);
      console.log('[Bitget Tracker] No balance history found. Page text snippet:', snippet);
      pushToTracker('balance_sniff', { url: 'DOM_SCRAPE_DEBUG', data: { pageTextSnippet: snippet } });
    }
  }

  // ── Auto-click tabs to load all data ────────────────────────────────────
  function clickTab(tabName) {
    const tabs = document.querySelectorAll('[role="tab"], [class*="tab"], [class*="Tab"], button, span, div');
    for (const el of tabs) {
      const text = (el.innerText || '').trim();
      if (text === tabName || text.toLowerCase() === tabName.toLowerCase()) {
        el.click();
        console.log('[Bitget Tracker] clicked tab:', tabName);
        return true;
      }
    }
    return false;
  }

  function autoTabCycle() {
    // 10s after load: click "Balance history"
    setTimeout(() => {
      clickTab('Balance history');

      // Wait 5s for content to render, THEN scrape
      setTimeout(() => {
        scrapeCopyDetails();

        // 5s later: click back to "Positions" for live tracking
        setTimeout(() => {
          clickTab('Positions');
        }, 5_000);
      }, 5_000);
    }, 10_000);
  }

  // Start polling after page load
  window.addEventListener('load', () => {
    activePoll();
    setInterval(activePoll, 60_000);

    // Scrape DOM every 30s
    setTimeout(scrapeCopyDetails, 5_000);
    setInterval(scrapeCopyDetails, 30_000);

    // Auto-cycle tabs: Balance history → Positions
    autoTabCycle();
  });

  console.log('[Bitget Tracker] v2.0 loaded — pushing to', TRACKER_URL);
})();
