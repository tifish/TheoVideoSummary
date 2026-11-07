// Use the extension bridge bundled with the same Python Playwright driver.
// This entry point is version-sensitive; requirements.txt pins the tested version.
'use strict';

const token = process.env.PLAYWRIGHT_MCP_EXTENSION_TOKEN;
let browser;
let stopping = false;
const startupTimeout = setTimeout(() => {
  console.log(JSON.stringify({error: 'Edge extension connection timed out; check the extension and token.'}));
  process.exit(1);
}, 45000);

async function stop() {
  if (stopping) return;
  stopping = true;
  clearTimeout(startupTimeout);
  // Unbind our bridge only. Never close the user's Edge browser.
  try { await browser?.unbind(); } catch {}
  process.exit(0);
}
process.stdin.resume();
process.stdin.on('end', stop);
process.on('SIGTERM', stop);
process.on('SIGINT', stop);

(async () => {
  if (!token) throw new Error('PLAYWRIGHT_MCP_EXTENSION_TOKEN is not configured.');
  const {tools} = require(process.argv[2]);
  const options = {extension: true, browser: 'msedge'};
  const config = await tools.resolveCLIConfigForMCP(options, process.env);
  const info = await tools.createBrowserWithInfo(
    config,
    {clientName: 'TheoVideoSummary', cwd: process.cwd()},
    options,
    {title: 'TheoVideoSummary', workspaceDir: process.cwd()}
  );
  browser = info.browser;
  await browser.unbind();
  const {endpoint} = await browser.bind('TheoVideoSummary', {host: '127.0.0.1', port: 0});
  clearTimeout(startupTimeout);
  console.log(JSON.stringify({endpoint}));
})().catch(error => {
  clearTimeout(startupTimeout);
  let message = String(error.message || error);
  if (token) message = message.split(token).join('[REDACTED]');
  message = message.replace(/([?&]token=)[^\s&]+/gi, '$1[REDACTED]');
  console.log(JSON.stringify({error: message}));
  process.exit(1);
});
