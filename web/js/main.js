// The application wiring: text -> model -> train -> receipt, all on one page.
//
// Nothing on this page is a mock: the asset bytes come from Lux3D through the
// bridge, the arm comes from the URDF, the training run is the engine's own
// `embodied_sim_collect`, and every number in the result panel is read from the
// files that run left behind.

import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { api, fetchArrayBuffer } from './api.js';
import { createStage } from './scene.js';
import { buildArm } from './arm.js';
import { parseStl } from './stl.js';

const $ = (id) => document.getElementById(id);
const fmt = (value, digits = 3) => (typeof value === 'number' ? value.toFixed(digits) : '—');
const pct = (value) => (typeof value === 'number' ? `${(value * 100).toFixed(1)}%` : '—');
const short = (value, head = 8) => (value ? `${String(value).slice(0, head)}…` : '—');

window.addEventListener('error', (event) => {
  const host = document.getElementById('job-log');
  if (host) host.textContent += `✖ ${event.message} (${event.filename}:${event.lineno})\n`;
});
window.addEventListener('unhandledrejection', (event) => {
  const host = document.getElementById('job-log');
  if (host) host.textContent += `✖ ${event.reason?.message || event.reason}\n`;
});

const state = {
  config: null,
  model: null,
  arm: null,
  stage: null,
  assets: [],
  receipts: [],
  pairs: [],
  runs: [],
  selected: null,
  assetRoot: null,
  stlRoot: null,
  bboxHelper: null,
  tab: 'generate',
  job: null,          // { id, kind, timer, started }
  activeRunId: null,
  playing: null,
};

const loader = new GLTFLoader();
const glbCache = new Map();
window.__app = state;

// ── boot ──────────────────────────────────────────────────────────────────

async function boot() {
  try {
    state.config = await api.config();
  } catch {
    $('offline').hidden = false;
    $('server-dot').className = 'dot bad';
    return;
  }
  $('server-dot').className = 'dot ok';
  $('chip-key').textContent = state.config.has_key ? 'key ok' : 'key missing';
  $('chip-key').className = `chip ${state.config.has_key ? 'ok' : 'warn'}`;
  $('urdf-source').textContent = state.config.urdf;
  renderEngine();

  state.stage = createStage($('view'));
  try {
    state.model = await api.robot(state.config.urdf);
  } catch (error) {
    appendLog(`✖ URDF: ${error.message}`);
    return;
  }
  state.arm = buildArm(state.model);
  state.stage.scene.add(state.arm.root);
  $('chip-robot').textContent = `${state.model.robot} · ${state.model.dof} dof`;
  $('chip-robot').className = 'chip ok';
  buildJointControls();
  updateTcp();

  fillForms();
  wireTabs();
  await refreshData();
  await refreshRuns();
  await selectAsset(state.selected);
  setTab('generate');
}

function renderEngine() {
  const engine = state.config.engine || {};
  $('engine-box').innerHTML = `<span class="dot ${engine.ok ? 'ok' : 'bad'}"></span><span id="engine-text">${
    engine.ok ? `engine · ${engine.path}` : `engine missing · ${engine.reason || 'not configured'}`
  }</span>`;
  $('chip-engine').textContent = engine.ok ? 'engine ok' : 'engine —';
  $('chip-engine').className = `chip ${engine.ok ? 'ok' : 'warn'}`;
  $('btn-train').disabled = !engine.ok;
  $('btn-train').title = engine.ok ? '' : 'start the bridge with --engine /path/to/lux3D-engine';
  $('btn-prepare').disabled = !state.selected;
}

// ── tabs / flow ───────────────────────────────────────────────────────────

function wireTabs() {
  for (const step of document.querySelectorAll('#flow .step')) {
    step.addEventListener('click', () => {
      if (step.dataset.step === 'train' && !state.selected) return;
      setTab(step.dataset.step);
    });
  }
}

function setTab(name) {
  state.tab = name;
  for (const step of document.querySelectorAll('#flow .step')) {
    step.classList.toggle('active', step.dataset.step === name);
  }
  for (const panel of document.querySelectorAll('.tab-panel')) {
    panel.hidden = panel.dataset.panel !== name;
  }
  if (name === 'train') {
    $('train-asset').value = state.selected ? state.selected.name : '';
  }
}

function markSteps() {
  const done = {
    generate: state.assets.length > 0,
    train: state.runs.length > 0,
    results: Boolean(state.activeRunId),
  };
  for (const step of document.querySelectorAll('#flow .step')) {
    step.classList.toggle('done', Boolean(done[step.dataset.step]));
  }
}

// ── robot ─────────────────────────────────────────────────────────────────

function buildJointControls() {
  const host = $('joints');
  host.innerHTML = '';
  for (const [name, joint] of state.arm.joints) {
    const lower = joint.limits.lower ?? -Math.PI;
    const upper = joint.limits.upper ?? Math.PI;
    const row = document.createElement('div');
    row.className = 'joint';
    row.innerHTML = `
      <label for="joint-${name}"><span>${name} · ${joint.type}</span><span class="val" id="val-${name}">0.00</span></label>
      <input id="joint-${name}" type="range" min="${lower}" max="${upper}" step="0.01" value="0" />
    `;
    host.appendChild(row);
    row.querySelector('input').addEventListener('input', (event) => {
      const value = Number(event.target.value);
      state.arm.setJoint(name, value);
      $(`val-${name}`).textContent = `${value.toFixed(2)} rad · ${((value * 180) / Math.PI).toFixed(0)}°`;
      updateTcp();
    });
  }
  $('btn-home').addEventListener('click', resetJoints);
  $('btn-play').addEventListener('click', togglePlay);
}

function resetJoints() {
  if (state.playing) togglePlay();
  for (const [name] of state.arm.joints) {
    state.arm.setJoint(name, 0);
    const input = $(`joint-${name}`);
    if (input) input.value = '0';
    const label = $(`val-${name}`);
    if (label) label.textContent = '0.00 rad · 0°';
  }
  updateTcp();
}

function togglePlay() {
  if (state.playing) {
    cancelAnimationFrame(state.playing);
    state.playing = null;
    $('btn-play').textContent = '演示动作';
    return;
  }
  $('btn-play').textContent = '停止';
  const start = performance.now();
  const names = [...state.arm.joints.keys()];
  const step = () => {
    const t = (performance.now() - start) / 1000;
    state.arm.setJoint(names[0], Math.sin(t * 1.25) * 0.55);
    if (names[1]) state.arm.setJoint(names[1], -0.55 + Math.cos(t * 0.9) * 0.45);
    for (const name of names) {
      const input = $(`joint-${name}`);
      if (input) input.value = String(state.arm.joints.get(name).value);
      const label = $(`val-${name}`);
      if (label) label.textContent = `${state.arm.joints.get(name).value.toFixed(2)} rad`;
    }
    updateTcp();
    state.playing = requestAnimationFrame(step);
  };
  state.playing = requestAnimationFrame(step);
}

function updateTcp() {
  const position = state.arm.worldPosition(state.arm.toolLink);
  if (!position) return;
  const [x, y, z] = [position.x, -position.z, position.y];
  $('stage-tcp').textContent = `TCP ${x.toFixed(3)} ${y.toFixed(3)} ${z.toFixed(3)} m`;
}

// ── assets ────────────────────────────────────────────────────────────────

async function refreshData() {
  const data = await api.assets();
  state.assets = data.assets || [];
  state.receipts = data.receipts || [];
  state.pairs = data.pairs || [];
  renderAssetList();
  $('chip-assets').textContent = `assets ${state.assets.length}`;
  $('chip-assets').className = `chip ${state.assets.length ? 'ok' : ''}`;
  if (!state.selected) state.selected = state.assets.find((a) => a.has_glb || a.has_stl) || null;
  markSteps();
  renderEngine();
}

function renderAssetList() {
  const host = $('asset-list');
  host.innerHTML = '';
  if (!state.assets.length) {
    host.innerHTML = '<p class="empty">还没有资产。用「生成」或 <code>python bridge.py demo</code>。</p>';
    return;
  }
  for (const entry of state.assets) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `asset${state.selected && state.selected.name === entry.name ? ' active' : ''}`;
    const size = entry.bbox_m?.size ? entry.bbox_m.size.map((v) => v.toFixed(2)).join(' × ') : '—';
    const missing = entry.has_glb ? '' : ' <span class="bad">glb missing</span>';
    button.innerHTML = `
      <div class="name">${entry.name}${missing}</div>
      <div class="meta">${entry.asset_id || '—'} · ${entry.triangles ?? '—'} tris · ${size} m</div>
      <div class="meta">${entry.dir}</div>
    `;
    button.addEventListener('click', () => selectAsset(entry));
    host.appendChild(button);
  }
}

async function selectAsset(entry) {
  if (!entry) {
    renderIdentity(null);
    return;
  }
  state.selected = entry;
  renderAssetList();
  renderIdentity(entry);
  $('stage-asset').textContent = `${entry.name} · ${entry.asset_id}`;
  $('stage-asset').className = 'tag accent';
  $('train-asset').value = entry.name;
  $('btn-prepare').disabled = false;

  disposeGroup(state.assetRoot);
  disposeGroup(state.stlRoot);
  if (state.bboxHelper) {
    state.stage.scene.remove(state.bboxHelper);
    state.bboxHelper = null;
  }
  state.assetRoot = null;
  state.stlRoot = null;

  if (entry.has_glb) {
    try {
      state.assetRoot = await loadGlb(entry);
      placeInFront(state.assetRoot);
      state.stage.scene.add(state.assetRoot);
      applyWireframe();
      appendLog(`glb · ${entry.name} · ${countTriangles(state.assetRoot)} tris on screen`);
      frameScene();
    } catch (error) {
      appendLog(`✖ GLB: ${error.message}`);
    }
  }
  if (entry.has_stl) {
    try {
      state.stlRoot = await loadCollision(entry);
      placeInFront(state.stlRoot, state.assetRoot);
      state.stage.scene.add(state.stlRoot);
    } catch (error) {
      appendLog(`✖ STL: ${error.message}`);
    }
  }
  applyToggles();
  markSteps();
}

async function loadGlb(entry) {
  const url = api.assetUrl(entry.name, 'glb');
  if (!glbCache.has(url)) glbCache.set(url, loader.loadAsync(url));
  const gltf = await glbCache.get(url);
  const root = gltf.scene.clone(true);
  root.traverse((node) => {
    if (!node.isMesh) return;
    node.castShadow = true;
    node.receiveShadow = true;
    if (node.material?.map) node.material.map.colorSpace = THREE.SRGBColorSpace;
  });
  return root;
}

async function loadCollision(entry) {
  const buffer = await fetchArrayBuffer(api.assetUrl(entry.name, 'stl'));
  const positions = parseStl(buffer);
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  geometry.computeVertexNormals();
  const mesh = new THREE.Mesh(
    geometry,
    new THREE.MeshStandardMaterial({
      color: 0xff7a3d, transparent: true, opacity: 0.1, roughness: 0.5,
      metalness: 0, depthWrite: false, side: THREE.DoubleSide,
    })
  );
  const edges = new THREE.LineSegments(
    new THREE.EdgesGeometry(geometry, 28),
    new THREE.LineBasicMaterial({ color: 0xff9a68, transparent: true, opacity: 0.55, depthTest: false })
  );
  const group = new THREE.Group();
  group.add(mesh, edges);
  group.renderOrder = 2;
  return group;
}

function placeInFront(group, reference) {
  const box = new THREE.Box3().setFromObject(reference || group);
  if (box.isEmpty()) return;
  const center = box.getCenter(new THREE.Vector3());
  const target = new THREE.Vector3(-0.46, 0, 0.26);
  group.position.x += target.x - center.x;
  group.position.z += target.z - center.z;
  group.position.y += -box.min.y;
  if (reference && reference !== group) group.position.copy(reference.position);
}

function countTriangles(root) {
  let total = 0;
  root.traverse((node) => {
    if (!node.isMesh || !node.geometry) return;
    const g = node.geometry;
    total += g.index ? g.index.count / 3 : g.attributes.position.count / 3;
  });
  return Math.round(total);
}

function frameScene() {
  const box = new THREE.Box3();
  box.expandByObject(state.arm.root);
  if (state.assetRoot) box.expandByObject(state.assetRoot);
  state.stage.frameOn(box, 1.5);
}

function renderIdentity(entry) {
  const host = $('identity');
  if (!entry) {
    host.innerHTML = '<p class="empty">select an asset</p>';
    return;
  }
  const size = entry.bbox_m?.size || [];
  const rows = [
    ['asset_id', entry.asset_id || '—'],
    ['triangles', entry.triangles ?? '—'],
    ['bbox m', size.length ? size.map((v) => fmt(v, 3)).join(' × ') : '—'],
    ['sha256', short(entry.source_sha256, 10)],
    ['stl', entry.has_stl ? `${(entry.stl_bytes / 1024).toFixed(0)} KiB` : 'missing'],
  ];
  host.innerHTML = rows
    .map(([k, v]) => `<div class="line"><span class="k">${k}</span><span class="v">${v}</span></div>`)
    .join('');
}

// ── view toggles ──────────────────────────────────────────────────────────

function applyToggles() {
  if (state.stlRoot) state.stlRoot.visible = $('toggle-collision').checked;
  if (state.assetRoot) {
    const box = new THREE.Box3().setFromObject(state.assetRoot);
    if (state.bboxHelper) state.stage.scene.remove(state.bboxHelper);
    state.bboxHelper = new THREE.Box3Helper(box, 0xffd166);
    state.bboxHelper.visible = $('toggle-bbox').checked;
    state.stage.scene.add(state.bboxHelper);
  }
  applyWireframe();
  state.stage.controls.autoRotate = $('toggle-spin').checked;
}

function applyWireframe() {
  const wire = $('toggle-wire').checked;
  if (!state.assetRoot) return;
  state.assetRoot.traverse((node) => {
    if (!node.isMesh || !node.material) return;
    for (const material of Array.isArray(node.material) ? node.material : [node.material]) {
      if ('wireframe' in material) material.wireframe = wire;
    }
  });
}

function disposeGroup(group) {
  if (!group) return;
  state.stage.scene.remove(group);
  group.traverse((node) => {
    if (node.geometry) node.geometry.dispose();
  });
}

// ── generation ────────────────────────────────────────────────────────────

function fillForms() {
  const style = $('gen-style');
  for (const value of state.config.styles || []) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = value;
    style.appendChild(option);
  }
  $('gen-dry').checked = !state.config.has_key;
  if (!state.config.has_key) {
    appendLog('no API key: dry-run only (the request body is printed, nothing is sent)');
  }
  $('gen-form').addEventListener('submit', submitGenerate);
  $('train-form').addEventListener('submit', submitTrain);
  $('btn-prepare').addEventListener('click', prepareOnly);
  $('btn-cancel').addEventListener('click', cancelJob);
  $('btn-clear-log').addEventListener('click', () => { $('job-log').textContent = ''; });
  $('btn-copy-cmd').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText($('train-command').textContent);
      appendLog('command copied · 命令已复制');
    } catch {
      appendLog('clipboard blocked by the browser');
    }
  });
  for (const id of ['toggle-collision', 'toggle-bbox', 'toggle-wire', 'toggle-spin']) {
    $(id).addEventListener('change', applyToggles);
  }
}

async function submitGenerate(event) {
  event.preventDefault();
  if (state.job) return;
  const payload = {
    prompt: $('gen-prompt').value.trim(),
    version: $('gen-version').value,
    face_count: Number($('gen-faces').value),
    style: $('gen-style').value || null,
    name: $('gen-name').value.trim() || 'lux3d',
    dry_run: $('gen-dry').checked,
    mode: 'text-to-3d',
  };
  $('btn-generate').disabled = true;
  try {
    const response = await api.generate(payload);
    startJob(response.job, 'generate');
  } catch (error) {
    appendLog(`✖ ${error.message}`);
    $('btn-generate').disabled = false;
  }
}

// ── training ──────────────────────────────────────────────────────────────

async function prepareOnly() {
  if (!state.selected) return;
  try {
    const response = await api.prepare({
      asset: state.selected.name,
      task: $('train-task').value,
      episodes: Number($('train-episodes').value),
      seed: Number($('train-seed').value),
    });
    showCommand(response.run);
    appendLog(`prepared ${response.dir} · mesh + URDF written`);
  } catch (error) {
    appendLog(`✖ prepare: ${error.message}`);
  }
}

async function submitTrain(event) {
  event.preventDefault();
  if (state.job) return;
  if (!state.selected) {
    appendLog('✖ pick an asset first');
    return;
  }
  $('btn-train').disabled = true;
  try {
    const response = await api.train({
      asset: state.selected.name,
      task: $('train-task').value,
      episodes: Number($('train-episodes').value),
      seed: Number($('train-seed').value),
      verify: $('train-verify').value,
    });
    startJob(response.job, 'train');
    appendLog(`training run ${response.run_id} started · 引擎编译/运行中`);
    setTab('train');
  } catch (error) {
    appendLog(`✖ train: ${error.message}`);
    $('btn-train').disabled = false;
  }
}

function showCommand(record) {
  if (!record || !record.command_line) return;
  $('train-command-wrap').hidden = false;
  $('train-command').textContent = record.command_line;
}

async function cancelJob() {
  if (!state.job) return;
  try {
    await api.cancel(state.job.id);
    appendLog('cancel requested · 已请求终止');
  } catch (error) {
    appendLog(`✖ cancel: ${error.message}`);
  }
}

// ── job polling (shared by generate and train) ────────────────────────────

function startJob(job, kind) {
  const elapsed = Math.max(0, Date.now() / 1000 - (job.created_at || Date.now() / 1000));
  state.job = { id: job.id, kind, timer: null, startedAt: Date.now() - elapsed * 1000 };
  if (kind === 'train') {
    $('run-progress').hidden = false;
    $('run-state').textContent = 'queued';
    $('run-elapsed').textContent = '0s';
    $('stage-run').hidden = false;
    $('stage-run').textContent = `run ${job.run_id || ''}`;
  }
  appendLog(`job ${job.id} · ${kind} · ${job.state}`);
  pollJob();
}

async function pollJob() {
  if (!state.job) return;
  let job;
  try {
    job = (await api.job(state.job.id)).job;
  } catch (error) {
    appendLog(`✖ ${error.message}`);
    finishJob();
    return;
  }
  if (Array.isArray(job.log)) $('job-log').textContent = job.log.join('\n');
  if (state.job.kind === 'train') updateProgress(job);

  if (job.state === 'success' || job.state === 'failed') {
    if (job.kind === 'generate' || job.payload?.prompt) {
      // generation finished: refresh the list and move to Train
      if (job.state === 'success' && job.asset) {
        appendLog(`✔ generated ${job.asset.name} · ${job.asset.triangles} tris · asset ${job.asset.asset_id}`);
        await refreshData();
        const fresh = state.assets.find((a) => a.name === job.asset.name);
        if (fresh) await selectAsset(fresh);
        setTab('train');
      } else if (job.state === 'success') {
        appendLog(`✔ ${job.message}`);
      }
    }
    if (job.kind === 'train') {
      if (job.state === 'success') {
        appendLog(`✔ ${job.message}`);
        await refreshRuns();
        if (job.run_id) await openRun(job.run_id);
        setTab('results');
      } else {
        appendLog(`✖ ${job.message}`);
      }
    }
    if (job.state === 'failed') appendLog(`✖ ${job.message}`);
    finishJob();
    return;
  }
  state.job.timer = setTimeout(pollJob, state.job.kind === 'train' ? 1500 : 2000);
}

function updateProgress(job) {
  $('run-state').textContent = job.state === 'running' ? 'training' : job.state;
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - state.job.startedAt / 1000));
  $('run-elapsed').textContent = `${seconds}s`;
  const last = (job.log || []).filter((line) => line.trim() && !line.startsWith('$')).slice(-1)[0] || '';
  $('run-lastline').textContent = last.slice(0, 160);
}

function finishJob() {
  if (state.job?.timer) clearTimeout(state.job.timer);
  state.job = null;
  $('btn-generate').disabled = false;
  $('btn-train').disabled = !state.config?.engine?.ok;
  $('run-progress').hidden = true;
  $('stage-run').hidden = true;
}

// ── runs / results ────────────────────────────────────────────────────────

async function refreshRuns() {
  const data = await api.runs();
  state.runs = data.runs || [];
  renderRuns();
  markSteps();
}

function renderRuns() {
  const host = $('runs');
  if (!state.runs.length) {
    host.innerHTML = '<p class="empty">还没有训练记录。到「训练」页跑一次。</p>';
    return;
  }
  host.innerHTML = '';
  for (const run of state.runs) {
    const result = run.result || {};
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `run${state.activeRunId === run.id ? ' active' : ''}`;
    const verdict = run.status === 'done'
      ? `<span class="rres ok">${result.successes ?? '?'}/${result.episodes ?? '?'}</span>`
      : `<span class="rres ${run.status === 'failed' ? 'bad' : ''}">${run.status}</span>`;
    button.innerHTML = `<span class="rid">${run.id}</span>${verdict}`;
    button.addEventListener('click', () => openRun(run.id));
    host.appendChild(button);
  }
}

async function openRun(runId) {
  try {
    const data = await api.run(runId);
    state.activeRunId = runId;
    renderRuns();
    renderResult(data.run);
    markSteps();
  } catch (error) {
    appendLog(`✖ run ${runId}: ${error.message}`);
  }
}

function renderResult(run) {
  const host = $('result');
  const result = run.result || {};
  const receipt = run.receipt || {};
  const rate = typeof result.success_rate === 'number' ? result.success_rate : null;
  const wilson = result.wilson95 ? `Wilson95 [${pct(result.wilson95[0])}, ${pct(result.wilson95[1])}]` : '';

  const frames = (run.frames || []).slice(0, 1).map((track) => {
    const match = /^(.+)\/(episode_\d+)\/frame_(\d+)\.png$/.exec(track.first || '');
    if (!match) return '';
    const total = track.frames_per_episode || 0;
    const picks = [...new Set([0, Math.floor(total / 2), Math.max(0, total - 1)])];
    const images = picks
      .map((index) => {
        const name = `frame_${String(index).padStart(6, '0')}.png`;
        const url = `/api/runs/${encodeURIComponent(run.id)}/frame/${match[1]}/${match[2]}/${name}`;
        return `<img src="${url}" alt="camera frame ${index}" loading="lazy" />`;
      })
      .join('');
    return `<div class="frames">${images}<div class="cap">${track.track} · ${track.episodes} episode(s) · ${total} frames each</div></div>`;
  }).join('');

  const rows = (result.runs || [])
    .map(
      (ep, index) => `<tr>
        <td>${index}</td>
        <td>${ep.seed ?? '—'}</td>
        <td class="${ep.success ? 'ok' : 'bad'}">${ep.success ? '✓' : '✗'}</td>
        <td>${ep.steps ?? '—'}</td>
        <td>${typeof ep.reward_sum === 'number' ? ep.reward_sum.toFixed(2) : '—'}</td>
        <td>${ep.failure_phase || ''}</td>
      </tr>`
    )
    .join('');

  host.innerHTML = `
    <div class="banner">
      <span class="big ${rate === null ? '' : rate > 0 ? 'ok' : 'bad'}">${rate === null ? '—' : pct(rate)}</span>
      <span class="sub">${result.successes ?? '?'}/${result.episodes ?? '?'} episodes · ${result.protocol || receipt.protocol || '—'}</span>
    </div>
    <div class="bar"><span style="width:${rate === null ? 0 : Math.max(2, rate * 100)}%"></span></div>
    <div class="kv">
      <div class="line"><span class="k">spec_hash</span><span class="v">${result.spec_hash || receipt.spec_hash || '—'}</span></div>
      <div class="line"><span class="k">wilson</span><span class="v">${wilson || '—'}</span></div>
      <div class="line"><span class="k">gate</span><span class="v">${result.gate || '—'}</span></div>
      <div class="line"><span class="k">verify</span><span class="v">${result.verify_determinism || '—'}</span></div>
      <div class="line"><span class="k">throughput</span><span class="v">${typeof result.throughput_steps_per_s === 'number' ? result.throughput_steps_per_s.toFixed(0) + ' steps/s' : '—'}</span></div>
      <div class="line"><span class="k">transitions</span><span class="v">${receipt.transitions ?? result.transitions ?? '—'}</span></div>
      <div class="line"><span class="k">mesh</span><span class="v">${run.mesh || '—'}</span></div>
      <div class="line"><span class="k">run dir</span><span class="v">${run.id}</span></div>
    </div>
    ${rows ? `<table class="episodes"><thead><tr><th>#</th><th>seed</th><th>ok</th><th>steps</th><th>reward</th><th>phase</th></tr></thead><tbody>${rows}</tbody></table>` : ''}
    ${frames}
  `;
}

// ── log ───────────────────────────────────────────────────────────────────

function appendLog(line) {
  const host = $('job-log');
  host.textContent += `${line}\n`;
  host.scrollTop = host.scrollHeight;
}

boot();
