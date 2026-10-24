/* Synthetic integration fixture only: run in the documented network-none
 * container with public.example mapped to its loopback. Never attach a profile.
 * argv[2] is JSON containing guarded_navigation() bodies from the tested source.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const https = require('node:https');
const os = require('node:os');
const path = require('node:path');
const zlib = require('node:zlib');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');
assert.equal(process.env.RIO_ISOLATED_BROWSER_LAB, '1', 'explicit disposable-lab flag required');
assert(Object.values(os.networkInterfaces()).flat().every(address => address.internal),
  'fixture requires a network-none container; never run it on the NAS network');
const guards = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'rio-route-'));
const hits = {};
let browser, server;
const execute = (page, body) => new Function('page', 'return(async()=>{' + body + '})()')(page);

(async () => {
  const key = path.join(temporary, 'key.pem'), cert = path.join(temporary, 'cert.pem');
  execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
    '-keyout', key, '-out', cert, '-subj', '/CN=public.example'], {stdio: 'ignore'});
  server = https.createServer({key: fs.readFileSync(key), cert: fs.readFileSync(cert)}, (req, res) => {
    hits[req.url] = (hits[req.url] || 0) + 1;
    if (req.url === '/redirect' || req.url === '/redirect-public') {
      res.writeHead(302, {location: req.url === '/redirect' ?
        'https://127.0.0.1/private' : 'https://public.example/destination'});
      return res.end();
    }
    res.setHeader('content-type', 'text/html');
    if (req.url === '/gzip') {
      res.setHeader('content-encoding', 'gzip');
      return res.end(zlib.gzipSync('<title>Compressed fixture</title>'));
    }
    res.end(req.url === '/normal' ? `<title>Rio fixture</title><script>
      setTimeout(()=>{
        fetch('https://127.0.0.1/late-private').catch(()=>{});
        fetch('/late-public').catch(()=>{});
        fetch('/post', {method:'POST',body:'fixture'}).catch(()=>{});
      },150);
    </script>` : '<title>Fixture response</title>');
  });
  await new Promise(resolve => server.listen(443, '127.0.0.1', resolve));
  browser = await chromium.launch({headless: true,
    executablePath: process.env.RIO_TEST_BROWSER_EXECUTABLE || undefined,
    args: ['--no-sandbox', '--disable-background-networking']});

  // A control proves the old continue()-based guard cannot stop this redirect.
  const oldContext = await browser.newContext({ignoreHTTPSErrors: true, serviceWorkers: 'block'});
  const oldPage = await oldContext.newPage();
  await oldPage.route('**/*', async route => new URL(route.request().url()).hostname === '127.0.0.1' ?
    route.abort() : route.continue());
  await oldPage.goto('https://public.example/redirect');
  assert.equal(hits['/private'], 1, 'control must demonstrate automatic redirect');
  await oldContext.close();
  for (const key of Object.keys(hits)) delete hits[key];

  const context = await browser.newContext({ignoreHTTPSErrors: true, serviceWorkers: 'block'});
  const page = await context.newPage();
  await execute(page, guards.normal);
  await page.waitForTimeout(600);
  assert.equal(await page.title(), 'Rio fixture');
  assert.equal(hits['/normal'], 1);
  assert.equal(hits['/late-public'], 1, 'legitimate delayed GET remains usable');
  assert.equal(hits['/late-private'] || 0, 0, 'guard must remain after navigation');
  assert.equal(hits['/post'] || 0, 0, 'research does not authorize POST');
  await execute(page, guards.gzip);
  assert.equal(await page.title(), 'Compressed fixture', 'response fulfillment preserves gzip content');
  for (const key of ['redirect', 'redirect-public']) {
    let rejected = false;
    try { await execute(page, guards[key]); } catch { rejected = true; }
    assert(rejected, 'redirect must fail closed');
  }
  assert.equal(hits['/private'] || 0, 0);
  assert.equal(hits['/destination'] || 0, 0);
  await context.close();
  console.log(JSON.stringify({status: 'PASS', browser: browser.version(),
    driver: require('playwright/package.json').version, baseline_private_hits: 1,
    guarded_private_hits: 0, public_get_and_delayed_get: true,
    post_blocked: true, all_redirects_blocked: true, gzip_response: true, hits}));
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (browser) await browser.close();
  if (server) await new Promise(resolve => server.close(resolve));
  fs.rmSync(temporary, {recursive: true, force: true});
});
