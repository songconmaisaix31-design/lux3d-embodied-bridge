// Wires the page: robot from the URDF, asset from the lock manifest, the
// receipt from the last bench run, and the Lux3D call behind the Generate button.

import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { api, fetchArrayBuffer } from './api.js';
import { createStage } from './scene.js';
import { buildArm } from './arm.js';
import { parseStl } from './stl.js';

const $ = (id) => document.getElementById(id);
const fmt = (value, digits = 3) => (typeof value === 'number' ? value.toFixed(digits) : '—');
const short = (value, head = 8) => (value ? `${String(value).slice(0, head)}…` : '—');

// Surface a broken module instead of leaving a blank canvas.
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
  selected: null,
  assetRoot: null,
  stlRoot: null,
  bboxHelper: null,
  jobTimer: null,
  jobId: null,
  logLines: 0,
  playing: null,
};

const loader = new GLTFLoader();
const glbCache = new Map();

// Exposed for the headless checks (tools/smoke-viewer.mjs) and for anyone
// debugging from the devtools console.
window.__viewer = state;

// ── boot ──────────────────────────────────────────────────────────────────

async function boot() {
  try {
    state.config = await api.config();
  } catch (error) {
    $('offline').hidden = false;
    $('server-dot').className = 'dot bad';
    return;
  }
  $('server-dot').className = 'dot ok';
  $('chip-key').textContent = state.config.has_key ? 'key ok' : 'key missing';
  $('chip-key').className = `chip ${state.config.has_key ? 'ok' : 'warn'}`;
  $('urdf-source').textContent = state.config.urdf;

  state.stage = createStage($('view'));

  try {
    state.model = await api.robot(state.config.urdf);
  } catch (error) {
    $('robot-note').textContent = `URDF failed: ${error.message}`;
    return;
  }
  state.arm = buildArm(state.model);
  state.stage.scene.add(state.arm.root);
  $('chip-robot').textContent = `${state.model.robot} · ${state.model.dof} dof`;
  $('chip-robot').className = 'chip ok';
  buildJointControls();
  updateTcp();

  fillGenerateForm();
  await refreshData();
  await selectAsset(state.selected);
}

async function refreshData() {
  const data = await api.assets();
  state.assets = data.assets || [];
  state.receipts = data.receipts || [];
  state.pairs = data.pairs || [];
  renderAssetList();
  $('chip-assets').textContent = `assets ${state.assets.length}`;
  $('chip-assets').className = `chip ${state.assets.length ? 'ok' : ''}`;
  const loadable = state.assets.filter((asset) => asset.has_glb || asset.has_stl);
  state.selected = loadable[0] || state.assets[0] || null;
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
  $('btn-home').addEventListener('click', () => resetJoints());
  $('btn-play').addEventListener('click', togglePlay);
}

function resetJoints() {
  if (state.playing) togglePlay();
  for (const [name, joint] of state.arm.joints) {
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
    const q1 = Math.sin(t * 1.25) * 0.55;
    const q2 = -0.55 + Math.cos(t * 0.9) * 0.45;
    state.arm.setJoint(names[0], q1);
    if (names[1]) state.arm.setJoint(names[1], q2);
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
  // scene is Y-up; URDF is Z-up (x, y, z) = (x, -z, y) in world space.
  const [x, y, z] = [position.x, -position.z, position.y];
  $('stage-tcp').textContent = `TCP ${x.toFixed(3)} ${y.toFixed(3)} ${z.toFixed(3)} m`;
}

// ── assets ────────────────────────────────────────────────────────────────

function renderAssetList() {
  const host = $('asset-list');
  host.innerHTML = '';
  if (!state.assets.length) {
    host.innerHTML = '<p class="empty">还没有资产。点右侧 Generate，或先跑 <code>python bridge.py demo</code>。</p>';
    return;
  }
  for (const entry of state.assets) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `asset${state.selected && state.selected.name === entry.name ? ' active' : ''}`;
    const size = entry.bbox_m?.size ? entry.bbox_m.size.map((value) => value.toFixed(2)).join(' × ') : '—';
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
    renderReceipt(null);
    return;
  }
  state.selected = entry;
  renderAssetList();
  renderIdentity(entry);
  renderReceipt(entry);
  $('stage-asset').textContent = `${entry.name} · ${entry.asset_id}`;
  $('stage-asset').className = 'tag accent';

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
      appendLog(`glb ok · ${entry.name} · ${countTriangles(state.assetRoot)} tris on screen`);
      frameScene();
    } catch (error) {
      appendLog(`✖ GLB failed: ${error.message}`);
    }
  }
  if (entry.has_stl) {
    try {
      state.stlRoot = await loadCollision(entry);
      placeInFront(state.stlRoot, state.assetRoot);
      state.stage.scene.add(state.stlRoot);
      appendLog(`stl ok · collision shell · ${(entry.stl_bytes / 1024).toFixed(0)} KiB`);
    } catch (error) {
      appendLog(`✖ STL failed: ${error.message}`);
    }
  }
  applyToggles();
}

function countTriangles(root) {
  let total = 0;
  root.traverse((node) => {
    if (!node.isMesh || !node.geometry) return;
    const geometry = node.geometry;
    total += geometry.index ? geometry.index.count / 3 : geometry.attributes.position.count / 3;
  });
  return Math.round(total);
}

function frameScene() {
  const box = new THREE.Box3();
  box.expandByObject(state.arm.root);
  if (state.assetRoot) box.expandByObject(state.assetRoot);
  state.stage.frameOn(box, 1.5);
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
    const material = node.material;
    if (material?.map) material.map.colorSpace = THREE.SRGBColorSpace;
    if (material) material.envMapIntensity = 0.8;
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
      color: 0xff7a3d,
      transparent: true,
      opacity: 0.1,
      roughness: 0.5,
      metalness: 0,
      depthWrite: false,
      side: THREE.DoubleSide,
    })
  );
  // Only the sharp edges: a full wireframe over ~15k triangles is noise, and the
  // point is "this is the hull the bench collides with", not a mesh dump.
  const edges = new THREE.LineSegments(
    new THREE.EdgesGeometry(geometry, 28),
    new THREE.LineBasicMaterial({ color: 0xff9a68, transparent: true, opacity: 0.55, depthTest: false })
  );
  const group = new THREE.Group();
  group.add(mesh, edges);
  group.renderOrder = 2;
  return group;
}

// Place the object on the table in front of the arm, keeping metres as metres.
function placeInFront(group, reference) {
  const box = new THREE.Box3().setFromObject(reference || group);
  if (box.isEmpty()) return;
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const target = new THREE.Vector3(-0.46, 0, 0.26);
  group.position.x += target.x - center.x;
  group.position.z += target.z - center.z;
  group.position.y += -box.min.y;
  if (reference && reference !== group) {
    group.position.copy(reference.position);
  }
  return size;
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
    ['bbox m', size.length ? size.map((value) => fmt(value, 3)).join(' × ') : '—'],
    ['source', entry.source_file || '—'],
    ['sha256', short(entry.source_sha256, 10)],
    ['stl sha256', short(entry.stl_sha256, 10)],
    ['glb', entry.has_glb ? `${(entry.glb_bytes / 1024).toFixed(0)} KiB` : 'missing'],
    ['stl', entry.has_stl ? `${(entry.stl_bytes / 1024).toFixed(0)} KiB` : 'missing'],
    ['manifest', entry.manifest],
  ];
  host.innerHTML = rows
    .map(([key, value]) => `<div class="line"><span class="k">${key}</span><span class="v">${value}</span></div>`)
    .join('');
}

function renderReceipt(entry) {
  const host = $('receipt');
  if (!state.receipts.length) {
    host.innerHTML = '<p class="empty">no receipt yet — run <code>python bridge.py bench --run</code>.</p>';
    return;
  }
  const receipt =
    state.receipts.find((item) => entry && item.file.startsWith(`${entry.dir}/`)) || state.receipts[0];
  const pair = state.pairs.find((item) => item.left === receipt.file || item.right === receipt.file);
  const rows = [
    ['file', receipt.file],
    ['scheme', receipt.scheme || '—'],
    ['spec_hash', receipt.spec_hash || '—'],
    ['protocol', receipt.protocol_id || '—'],
    ['episodes', receipt.episodes ?? '—'],
    ['transitions', receipt.transitions ?? '—'],
    ['successes', `${receipt.successes ?? '—'} (${((receipt.success_rate ?? 0) * 100).toFixed(1)}%)`],
    ['engine', receipt.engine_commit || '—'],
  ];
  const verdict = pair
    ? `<div class="verdict ${pair.match ? 'pass' : 'fail'}">${pair.match ? '✔ MATCH' : '✖ DIVERGENCE'} · ${pair.left.split('/').pop()} vs ${pair.right.split('/').pop()}${
        pair.match ? '' : `<br />${pair.first_difference}`
      }</div>`
    : '<div class="verdict">single receipt · 无第二份可对拍</div>';
  host.innerHTML =
    rows.map(([key, value]) => `<div class="line"><span class="k">${key}</span><span class="v">${value}</span></div>`).join('') +
    verdict;
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
  const walk = (root) => {
    if (!root) return;
    root.traverse((node) => {
      if (!node.isMesh || !node.material) return;
      for (const material of Array.isArray(node.material) ? node.material : [node.material]) {
        if ('wireframe' in material) material.wireframe = wire;
      }
    });
  };
  walk(state.assetRoot);
}

function disposeGroup(group) {
  if (!group) return;
  state.stage.scene.remove(group);
  group.traverse((node) => {
    if (node.geometry) node.geometry.dispose();
  });
}

// ── generate ──────────────────────────────────────────────────────────────

function fillGenerateForm() {
  const style = $('gen-style');
  for (const value of state.config.styles || []) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = value;
    style.appendChild(option);
  }
  $('gen-faces').value = '20000';
  $('gen-dry').checked = !state.config.has_key;
  $('gen-form').addEventListener('submit', submitGenerate);
  if (!state.config.has_key) {
    $('job-log').textContent = 'no API key: dry-run only (the request body is printed, nothing is sent)';
  }
}

async function submitGenerate(event) {
  event.preventDefault();
  if (state.jobId) return;
  const payload = {
    prompt: $('gen-prompt').value.trim(),
    version: $('gen-version').value,
    face_count: Number($('gen-faces').value),
    style: $('gen-style').value || null,
    name: $('gen-name').value.trim() || 'lux3d',
    dry_run: $('gen-dry').checked,
    mode: 'text-to-3d',
  };
  const button = $('btn-generate');
  button.disabled = true;
  state.logLines = 0;
  $('job-log').textContent = '';
  try {
    const response = await api.generate(payload);
    state.jobId = response.job.id;
    appendLog(`job ${state.jobId} · ${response.job.state}${payload.dry_run ? ' · dry-run' : ''}`);
    pollJob();
  } catch (error) {
    appendLog(`✖ ${error.message}`);
    button.disabled = false;
  }
}

async function pollJob() {
  if (!state.jobId) return;
  let job;
  try {
    const response = await api.job(state.jobId);
    job = response.job;
  } catch (error) {
    appendLog(`✖ ${error.message}`);
    finishJob();
    return;
  }
  for (const line of (job.log || []).slice(state.logLines)) appendLog(`· ${line}`);
  state.logLines = (job.log || []).length;
  if (job.state === 'success' || job.state === 'failed') {
    if (job.state === 'success') {
      appendLog(job.dry_run ? '✔ dry-run ok — nothing was sent' : `✔ ${job.message}`);
      await refreshData();
      if (job.asset) {
        const fresh = state.assets.find((item) => item.name === job.asset.name);
        if (fresh) await selectAsset(fresh);
      }
    } else {
      appendLog(`✖ ${job.message}`);
    }
    finishJob();
    return;
  }
  state.jobTimer = setTimeout(pollJob, 2000);
}

function finishJob() {
  state.jobId = null;
  state.jobTimer = null;
  $('btn-generate').disabled = false;
}

function appendLog(line) {
  const host = $('job-log');
  host.textContent += `${line}\n`;
  host.scrollTop = host.scrollHeight;
}

// ── start ─────────────────────────────────────────────────────────────────

for (const id of ['toggle-collision', 'toggle-bbox', 'toggle-wire', 'toggle-spin']) {
  $(id).addEventListener('change', applyToggles);
}

boot();
