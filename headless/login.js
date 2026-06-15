require('dotenv').config();
const puppeteer = require('puppeteer');
const path = require('path');
const readline = require('readline');

const BITGET_PAGE = process.env.BITGET_PAGE || 'https://www.bitget.com/copy-trading/mt5/follower/detail?portfolioId=YOUR_PORTFOLIO_ID';
const USER_DATA_DIR = path.join(__dirname, 'browser-data');

async function main() {
  console.log('Launching browser for login...');
  console.log('Browser data will be saved to:', USER_DATA_DIR);

  const browser = await puppeteer.launch({
    headless: false,
    userDataDir: USER_DATA_DIR,
    args: ['--no-sandbox', '--disable-setuid-sandbox'],
    defaultViewport: null,
  });

  const page = await browser.newPage();
  await page.goto(BITGET_PAGE, { waitUntil: 'networkidle2', timeout: 60_000 });

  console.log('\n========================================');
  console.log('  Log in to Bitget in the browser.');
  console.log('  When done, press ENTER here to save');
  console.log('  the session and close the browser.');
  console.log('========================================\n');

  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  await new Promise(resolve => rl.question('Press ENTER when logged in... ', resolve));
  rl.close();

  const title = await page.title();
  console.log('Page title:', title);
  console.log('Session saved! You can now run: npm start');

  await browser.close();
}

main().catch(e => {
  console.error('Error:', e);
  process.exit(1);
});
