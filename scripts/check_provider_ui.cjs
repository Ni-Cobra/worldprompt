// Run with Playwright available to Node; optionally set WORLDPROMPT_PLAYWRIGHT
// to its module path and WORLDPROMPT_CHROMIUM to a Chromium executable.
const { chromium } = require(process.env.WORLDPROMPT_PLAYWRIGHT || 'playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({headless: true,
    ...(process.env.WORLDPROMPT_CHROMIUM ? {executablePath: process.env.WORLDPROMPT_CHROMIUM} : {}),
    args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 1000}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let state = {version: '0.0.0-fixture', config: {provider:'codex', codex_command:[], claude_command:[],
      codex_model:'gpt-6-astra', claude_model:'sonnet', codex_reasoning_effort:'medium', claude_reasoning_effort:'medium',
      codex_fast_mode:false, claude_fast_mode:false, timeout_seconds:3600, high_timeout_seconds:3600, max_operations:512,
      world_concurrency:2, world_max_pending:32, world_lease_seconds:120, world_max_jobs_per_player:0, port:8765, world_port:8768,
      jobs_dir:'/tmp/jobs', world_token_file:'', minecraft_dir:''},
      config_path:'/tmp/settings.json', providers:{}, models:{codex:{items:[]},claude:{items:[]}},
      service:{world_url:'http://127.0.0.1:8768'}, jobs:[], recent_jobs:[], cleanup_job_ids:[]};
    let outcome = 'pending', checks = [], saves = [], modelRequests = [];
    let delaySave = false, releaseSave, saveEntered = () => {};
    const path = provider => [`C:\\Installed Apps\\${provider}.exe`];
    function finish(provider, ready) {
      state.providers[provider] = {...state.providers[provider], checking:false, ready,
        ...(ready ? {command:path(provider)} : {}), message:ready ? 'Signed in' : 'CLI failed its connection check.'};
    }
    await page.route('http://127.0.0.1:9876/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/state') return route.fulfill({json:state});
      if (url.pathname === '/api/providers/check') {
        const body = route.request().postDataJSON();
        checks.push(body);
        state.providers[body.provider] = {ready:false, checking:true, check_id:String(checks.length),
          requested_command:body.command, message:'Checking'};
        if (outcome !== 'pending') finish(body.provider, outcome === 'success');
        return route.fulfill({json:state, status:202});
      }
      if (url.pathname === '/api/config') {
        saves.push(route.request().postDataJSON());
        state.config = saves.at(-1); state.providers = {};
        if (delaySave) await new Promise(resolve => { releaseSave = resolve; saveEntered(); });
        return route.fulfill({json:state});
      }
      if (url.pathname === '/api/providers/models') {
        modelRequests.push(route.request().postDataJSON());
        return route.fulfill({json:state,status:202});
      }
      const filename = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
      return route.fulfill({body:fs.readFileSync('src/worldprompt_core/static/'+filename),
        contentType:filename.endsWith('.js')?'text/javascript':filename.endsWith('.css')?'text/css':'text/html'});
    });
    await page.goto('http://127.0.0.1:9876/#session=fixture');
    const codex = page.locator('[name=codex_command]');
    const claude = page.locator('[name=claude_command]');
    const dialog = page.locator('#command-warning');
    const check = page.locator('#check-provider');
    await page.waitForFunction(() => document.querySelector('#codex-status').textContent === 'Checking…');
    assert.equal(await codex.inputValue(), '');
    assert.equal(await claude.inputValue(), '');
    assert.deepEqual(checks[0], {provider:'codex',command:[]});
    finish('codex', true); await page.evaluate(() => poll());
    assert.equal(await codex.inputValue(), JSON.stringify(path('codex')));
    assert.match(await page.locator('.save-hint').first().textContent(), /unsaved/);
    await codex.evaluate(input => input.closest('details').open = true);
    await codex.fill('["missing-cli","--custom"]');
    outcome = 'failure'; await check.click(); await dialog.waitFor();
    assert.deepEqual(checks.at(-1).command, ['missing-cli','--custom']);
    await page.locator('#command-warning-ok').click();
    await page.evaluate(() => poll());
    assert.equal(await dialog.isVisible(), false);
    assert.equal(await codex.inputValue(), '["missing-cli","--custom"]');
    await check.click(); await dialog.waitFor();
    outcome = 'success'; await page.locator('#command-warning-find').click();
    await page.waitForFunction(expected => document.querySelector('[name=codex_command]').value === expected, JSON.stringify(path('codex')));
    assert.deepEqual(checks.at(-1).command, []);
    assert.equal(saves.length, 0, 'Checking/recovery must not save unrelated edits');
    // Automatic search failure retains the user's original command.
    await codex.fill('["still-missing"]'); outcome = 'failure';
    await check.click(); await dialog.waitFor();
    await page.locator('#command-warning-find').click();
    await page.waitForFunction(() => document.querySelector('#notice').classList.contains('error'));
    assert.equal(await codex.inputValue(), '["still-missing"]');
    assert.equal(await dialog.isVisible(), false);
    // Invalid JSON offers the same recovery without sending an invalid command.
    await codex.fill('not JSON'); const before = checks.length;
    await check.click(); await dialog.waitFor();
    assert.equal(checks.length, before);
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    const bounds = await dialog.boundingBox();
    assert(bounds.x >= 0 && bounds.x + bounds.width <= 390);
    assert(bounds.y >= 0 && bounds.y + bounds.height <= 844);
    await page.screenshot({path:'work/provider-warning-mobile.png'});
    await page.locator('#command-warning-ok').click();
    // An in-flight automatic response must not overwrite newer typing.
    await codex.fill(''); outcome = 'pending'; await check.click();
    await page.waitForFunction(() => document.querySelector('#codex-status').textContent === 'Checking…');
    await codex.fill('["newer-edit"]'); finish('codex',true); await page.evaluate(() => poll());
    assert.equal(await codex.inputValue(), '["newer-edit"]');
    // Switching providers discovers the other empty field.
    outcome = 'success'; await page.locator('[name=provider][value=claude]').check();
    await page.waitForFunction(expected => document.querySelector('[name=claude_command]').value === expected, JSON.stringify(path('claude')));
    assert.equal(await codex.inputValue(), '["newer-edit"]');
    // Blank form values persist as [] and remain valid settings.
    await claude.evaluate(input => input.closest('details').open = true);
    await claude.fill(''); outcome = 'pending'; await page.locator('#save').click();
    await page.waitForFunction(() => document.querySelector('#notice-text').textContent.startsWith('Settings saved'));
    assert.equal(await page.locator('.save-hint').first().textContent(), '');
    assert.deepEqual(saves.at(-1).claude_command, []);
    assert.deepEqual(saves.at(-1).codex_command, ['newer-edit']);
    // Saving during a check invalidates it and must allow a fresh check to finish.
    await page.waitForFunction(() => document.querySelector('#claude-status').textContent === 'Checking…');
    const checksBeforeSave = checks.length;
    outcome = 'success'; await page.locator('#save').click();
    await page.waitForFunction(() => document.querySelector('#claude-status').textContent === 'Account connected');
    assert(checks.length > checksBeforeSave);
    assert.equal(await check.isEnabled(), true);
    // Refresh uses the unsaved executable without saving any other edits, even
    // while a custom model field is incomplete.
    await claude.fill('["unsaved-cli"]');
    await page.locator('[name=claude_model]').selectOption(' custom');
    const savesBeforeRefresh = saves.length;
    await page.locator('[data-refresh-models=claude]').click();
    await page.waitForFunction(() => pendingModels.size === 0);
    assert.deepEqual(modelRequests.at(-1), {provider:'claude',command:['unsaved-cli']});
    assert.equal(saves.length, savesBeforeRefresh);
    assert.equal(await claude.inputValue(), '["unsaved-cli"]');
    assert.match(await page.locator('.save-hint').first().textContent(), /unsaved/);
    await page.locator('[name=claude_model]').selectOption('sonnet');
    // A slow save must not discard settings typed while its response is pending.
    await claude.fill('["submitted-cli"]');
    delaySave = true; outcome = 'pending';
    const saveIsWaiting = new Promise(resolve => { saveEntered = resolve; });
    await page.locator('#save').click();
    await saveIsWaiting;
    assert.equal(typeof releaseSave, 'function');
    await claude.fill('["newer-unsaved-cli"]');
    releaseSave(); delaySave = false;
    await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('newer edits'));
    assert.deepEqual(saves.at(-1).claude_command, ['submitted-cli']);
    assert.equal(await claude.inputValue(), '["newer-unsaved-cli"]');
    assert.match(await page.locator('.save-hint').first().textContent(), /unsaved/);
    // Reasoning effort lists each model's reported levels and follows Refresh models.
    const effort = page.locator('[name=claude_reasoning_effort]');
    const levels = () => effort.evaluate(select => [...select.options].map(option => option.value));
    const item = (id, reasoning_efforts) => ({id, name:id, supports_images:true, reasoning_efforts});
    state.models.claude = {items:[item('opus',['low','medium','high','xhigh','max']),
      item('sonnet',['low','medium','high','max']), item('haiku',[])]};
    await page.evaluate(() => poll());
    assert.deepEqual(await levels(), ['low','medium','high','max']);
    assert.equal(await effort.inputValue(), 'medium');
    await page.locator('[name=claude_model]').selectOption('opus');
    assert.deepEqual(await levels(), ['low','medium','high','xhigh','max']);
    await effort.selectOption('xhigh');
    // A model without the chosen level falls back to the nearest lower level it reports.
    await page.locator('[name=claude_model]').selectOption('sonnet');
    assert.equal(await effort.inputValue(), 'high');
    await page.locator('[name=claude_model]').selectOption('haiku');
    assert.deepEqual(await levels(), ['high']);
    assert.match(await page.locator('#claude-efforts-message').textContent(), /no reasoning effort levels/);
    await page.locator('[name=claude_model]').selectOption('sonnet');
    state.models.claude.items[1] = item('sonnet',['low','medium','high','xhigh','max']);
    await page.locator('[data-refresh-models=claude]').click();
    await page.waitForFunction(() => pendingModels.size === 0);
    assert.deepEqual(await levels(), ['low','medium','high','xhigh','max']);
    assert.equal(await effort.inputValue(), 'high');
    // Before the CLI lists models, the saved level is kept as the only choice.
    assert.deepEqual(await page.locator('[name=codex_reasoning_effort]').evaluate(select =>
      [...select.options].map(option => [option.value, option.text])), [['medium','Medium']]);
    assert.deepEqual(errors, []);
    console.log('Provider UI passed: discovery/recovery, both providers, mobile dialog, save/check invalidation, unsaved model refresh, per-model reasoning effort and edits preserved during saving.');
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode=1;});
