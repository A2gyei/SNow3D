// Automated headless check of this project's own local WebGL application.
//
// Verifies the Jiangjunshan scenario end to end: terrain load, vertical
// exaggeration, the three display modes, release-zone selection and playback.
// Set SNOWWORK_RUN_CHECK=1 to also submit a real com1DFA run for another zone
// (that costs about a minute of solver time and publishes a new scenario).
import { spawn } from 'node:child_process';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
const root = process.cwd();
const out = path.join(root, '.cache', 'browser-check');
await mkdir(out, { recursive: true });
const runCheck = process.env.SNOWWORK_RUN_CHECK === '1';
const chrome = spawn('C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe', [
  '--headless=new', '--no-first-run', '--no-default-browser-check', '--remote-debugging-port=9228',
  `--user-data-dir=${path.join(out, 'profile-' + Date.now())}`, '--window-size=1500,950', 'about:blank',
], { windowsHide: true, stdio: 'ignore' });
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
let socket;
try {
  let version, connectionError;
  for (let i = 0; i < 40; i++) {
    try { version = await (await fetch('http://127.0.0.1:9228/json/version', {signal:AbortSignal.timeout(1000)})).json(); break; }
    catch (error) { connectionError = error.cause?.code || error.message; await wait(250); }
  }
  if (!version) throw new Error(`Cannot connect to headless Chrome (${connectionError}). Check local socket permissions, or open start.cmd to inspect the page manually.`);
  const page = await (await fetch('http://127.0.0.1:9228/json/new?http://127.0.0.1:8765', {method:'PUT'})).json();
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
  let seq = 0;
  const pending = new Map();
  const errors = [];
  socket.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) { pending.get(message.id)(message); pending.delete(message.id); }
    if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails);
    if (message.method === 'Log.entryAdded' && message.params.entry.level === 'error') errors.push(message.params.entry);
  };
  const send = (method, params={}) => new Promise((resolve, reject) => {
    const id = ++seq;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 15000);
    pending.set(id, value => { clearTimeout(timer); value.error ? reject(new Error(JSON.stringify(value.error))) : resolve(value.result); });
    socket.send(JSON.stringify({id,method,params}));
  });
  const evaluate = async expression => {
    const result = await send('Runtime.evaluate', {expression, returnByValue:true, awaitPromise:true});
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  };
  const shot = async name => {
    const image = await send('Page.captureScreenshot', {format:'png'});
    await writeFile(path.join(out, name), Buffer.from(image.data, 'base64'));
  };
  await send('Runtime.enable');
  await send('Log.enable');
  await send('Page.enable');
  let ready = false;
  for (let i = 0; i < 150; i++) {
    ready = await evaluate('Boolean(window.__snowwork?.state.loaded)');
    if (ready) break;
    await wait(200);
  }
  if (!ready) throw new Error(await evaluate('document.body.innerText'));
  await wait(1500);
  const loading = await evaluate('({hidden:document.getElementById("loading").hidden, message:document.getElementById("loading-message").textContent})');
  if (!loading.hidden) throw new Error(`Rendering failed: ${loading.message}`);

  const terrain = await evaluate(`(() => {
    const s = window.__snowwork.state;
    return {terrain: s.manifest.terrain, cell: s.manifest.nativeCellSize, vertical: s.vertical,
      exaggeration: s.manifest.verticalExaggeration, frames: s.manifest.frames.length,
      zone: s.zone, candidates: (s.zones?.candidates || []).length,
      zonesRendered: s.manifest.overlays.filter(o => o.kind === 'candidate').length,
      gpu: Boolean(document.querySelector('canvas')),
      cesium: Cesium.VERSION, primitives: window.__snowwork.viewer.scene.primitives.length};
  })()`);
  if (terrain.terrain !== 'jiangjunshan') throw new Error(`Expected the Jiangjunshan terrain, got ${terrain.terrain}`);
  if (terrain.vertical !== 1.5) throw new Error(`Vertical exaggeration should default to 1.5, got ${terrain.vertical}`);
  if (terrain.candidates < 1 || terrain.zonesRendered !== terrain.candidates) throw new Error('Release-zone screening did not reach the scene');
  if (!terrain.zone) throw new Error('No release zone selected');

  // Vertical exaggeration must stay a display transform.
  await evaluate('(() => {const el=document.getElementById("vertical-scale"); el.value="2.6"; el.dispatchEvent(new Event("input"));})()');
  await wait(400);
  const vertical = await evaluate('({value:window.__snowwork.state.vertical, badge:document.getElementById("vertical-badge").textContent, relief:window.__snowwork.state.manifest.heightMax - window.__snowwork.state.manifest.heightMin})');
  if (vertical.value !== 2.6 || !vertical.badge.includes('2.6')) throw new Error('Vertical exaggeration slider failed');

  // Three display modes must survive the terrain change.
  const modes = {};
  for (const mode of ['speed', 'depth', 'snow']) {
    await evaluate(`document.querySelector("[data-mode=${mode}]").click()`);
    await wait(250);
    modes[mode] = await evaluate('({legend:document.getElementById("legend-title").textContent, selected:document.querySelector("#render-modes .selected").dataset.mode})');
    if (modes[mode].selected !== mode) throw new Error(`Display mode ${mode} did not activate`);
  }

  await evaluate('document.getElementById("play-button").click()');
  await wait(1200);
  const playing = await evaluate('({playing:window.__snowwork.state.playing, time:window.__snowwork.state.time})');
  if (!playing.playing || playing.time <= .1) throw new Error('Playback did not advance');
  await evaluate('document.getElementById("play-button").click();document.getElementById("timeline").value=40;document.getElementById("timeline").dispatchEvent(new Event("input"))');
  await wait(500);
  const scrub = await evaluate('({time:window.__snowwork.state.time, depth:document.getElementById("metric-depth").textContent, speed:document.getElementById("metric-speed").textContent, frame:document.getElementById("frame-label").textContent})');
  if (scrub.time !== 40) throw new Error('Timeline seek failed');
  await shot('jiangjunshan-oblique.png');

  // Selecting another candidate must retarget the highlight and the compute form.
  const target = await evaluate('(() => {const cards=[...document.querySelectorAll(".zone-card")]; const card=cards[Math.min(2,cards.length-1)]; card.click(); return card.dataset.zone;})()');
  await wait(400);
  const selection = await evaluate('({zone:window.__snowwork.state.zone, select:document.getElementById("compute-zone").value})');
  if (selection.zone !== target || selection.select !== target) throw new Error('Zone selection did not propagate');
  await evaluate('document.getElementById("view-top").click()');
  await wait(600);
  await shot('jiangjunshan-top.png');
  await evaluate('document.getElementById("view-oblique").click()');

  // The synthetic avaParabola examples must stay usable as a reference.
  const waitForScenario = async id => {
    for (let i = 0; i < 40; i++) {
      const loaded = await evaluate(`window.__snowwork.state.scenario === ${JSON.stringify(id)} && document.getElementById("loading").hidden`);
      if (loaded) return true;
      await wait(250);
    }
    return false;
  };
  await evaluate('window.__snowwork.loadScenario("baseline")');
  if (!await waitForScenario('baseline')) throw new Error('Legacy synthetic scenario failed to load');
  const legacy = await evaluate(`(() => {const s = window.__snowwork.state;
    return {scenario: s.scenario, terrain: s.manifest.terrain ?? null, vertical: s.vertical,
      frames: s.manifest.frames.length};})()`);
  if (legacy.scenario !== 'baseline') throw new Error('Legacy synthetic scenario failed to load');
  await evaluate('window.__snowwork.loadScenario("jiangjunshan_r01")');
  if (!await waitForScenario('jiangjunshan_r01')) throw new Error('Could not return to the Jiangjunshan scenario');
  const restored = await evaluate('({scenario:window.__snowwork.state.scenario, zone:window.__snowwork.state.zone})');
  if (restored.scenario !== 'jiangjunshan_r01') throw new Error('Could not return to the Jiangjunshan scenario');

  let submitted = null;
  if (runCheck) {
    await evaluate(`(() => {
      document.getElementById("toggle-compute").click();
      document.getElementById("thickness").value = "1.2";
      document.getElementById("compute-zone").value = "${target}";
      document.getElementById("compute-form").requestSubmit();
    })()`);
    await wait(1500);
    const task = await evaluate('({visible:!document.getElementById("job-panel").hidden, id:document.getElementById("job-id").textContent, disabled:document.getElementById("run-button").disabled})');
    if (!task.visible || !task.id || !task.disabled) throw new Error('Submitting a new physical scenario failed');
    let phase = 'queued';
    for (let i = 0; i < 200; i++) {
      const job = await evaluate(`fetch("/api/jobs/${task.id}").then(r=>r.json())`);
      phase = job.phase;
      if (phase === 'complete' || phase === 'failed') break;
      await wait(3000);
    }
    if (phase !== 'complete') throw new Error(`Background run ended as ${phase}`);
    await wait(2500);
    submitted = await evaluate('({scenario:window.__snowwork.state.scenario, zone:window.__snowwork.state.manifest.zone?.id, title:document.getElementById("scene-title").textContent, job:document.getElementById("job-panel").hidden})');
    if (submitted.zone !== target) throw new Error(`New scenario ran on ${submitted.zone} instead of ${target}`);
    await shot('jiangjunshan-new-run.png');
  }

  const result = {terrain, vertical, modes, playing, scrub, selection, legacy, restored, submitted, errors};
  await writeFile(path.join(out,'result.json'), JSON.stringify(result,null,2));
  console.log(JSON.stringify(result));
  if (errors.length) process.exitCode = 1;
} catch (error) {
  console.error(error);
  process.exitCode = 1;
} finally {
  socket?.close();
  chrome.kill();
}
