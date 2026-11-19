// Lux3D 机械臂工作台: workbench controller.
// Owns UI state, same-origin API calls, job polling and replay playback.
// Rendering is delegated to /js/workbench-stage.js.
import { createWorkbenchStage } from '/js/workbench-stage.js';

const $ = (id) => document.getElementById(id);

const el = {
  apiStatus: $('api-status'),
  armStatus: $('arm-status'),
  assetList: $('asset-list'),
  viewport: $('viewport'),
  viewLabel: $('view-label'),
  viewportEmpty: $('viewport-empty'),
  fitView: $('fit-view'),
  collisionToggle: $('collision-toggle'),
  workspaceAlert: $('workspace-alert'),
  generateForm: $('generate-form'),
  prompt: $('prompt'),
  assetName: $('asset-name'),
  generateBtn: $('generate-btn'),
  generationStatus: $('generation-status'),
  sceneForm: $('scene-form'),
  orientation: $('asset-orientation'),
  widthMm: $('width-mm'),
  assetDetails: $('asset-details'),
  prepareBtn: $('prepare-btn'),
  sceneStatus: $('scene-status'),
  trainBtn: $('train-btn'),
  trainingStage: $('training-stage'),
  trainingProgress: $('training-progress'),
  lossChart: $('loss-chart'),
  jobLog: $('job-log'),
  cancelBtn: $('cancel-btn'),
  runList: $('run-list'),
  evaluationSummary: $('evaluation-summary'),
  episodeList: $('episode-list'),
  playReplay: $('play-replay'),
  seekReplay: $('seek-replay'),
  replayLabel: $('replay-label'),
};

const STAGES = ['generate', 'scene', 'train', 'review'];
const STEPS_PER_SECOND = 60;
const POLL_MS = 1200;
const POLL_MAX_BACKOFF_MS = 10000;
const LOG_LIMIT = 200;

const TERMINAL_OK = new Set(['succeeded', 'success', 'completed', 'done']);
const TERMINAL_FAIL = new Set(['failed', 'error']);
const TERMINAL_CANCEL = new Set(['cancelled', 'canceled']);
const isTerminal = (s) => TERMINAL_OK.has(s) || TERMINAL_FAIL.has(s) || TERMINAL_CANCEL.has(s);

const TRAIN_PHASE = {
  queued: '排队中',
  collecting: '采集示范',
  training: '训练策略',
  evaluating: '评估中',
  running: '运行中',
};

const state = {
  config: null,
  workbenchReady: false,
  assets: [],
  selectedAssetId: null,
  assetToken: 0,        // guards async asset preview races
  sceneToken: 0,        // guards async scene preparation races
  scenePending: false,
  run: null,            // currently prepared run (scene stage)
  sceneLoaded: false,   // renderer accepted run.scene
  activeSceneId: null,
  job: null,            // { id, kind: 'generate' | 'train', timer, failures }
  lossPoints: [],
  logLines: [],
  runs: [],
  reviewRun: null,
  reviewToken: 0,
  replay: null,         // { runId, seed, steps, frame, playing, rafId, lastTs, acc }
  stage: null,          // renderer controller
  stageError: null,
};

/* ---------------- API ---------------- */

class ApiError extends Error {
  constructor(message, status, network = false) {
    super(message);
    this.status = status;
    this.network = network;
  }
}

async function api(path, options = {}) {
  if (!path.startsWith('/') || path.startsWith('//')) {
    throw new ApiError('仅允许同源请求', 0);
  }
  const init = { method: options.method || 'GET', credentials: 'same-origin', headers: { Accept: 'application/json' } };
  if (options.body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(options.body);
  }
  if (options.signal) init.signal = options.signal;
  let res;
  try {
    res = await fetch(path, init);
  } catch (err) {
    if (err && err.name === 'AbortError') throw err;
    throw new ApiError('网络连接失败，请检查服务是否在运行', 0, true);
  }
  let data = null;
  const text = await res.text();
  if (text) {
    try { data = JSON.parse(text); } catch { data = null; }
  }
  if (!res.ok) {
    const msg = (data && typeof data.error === 'string' && data.error) || `请求失败（HTTP ${res.status}）`;
    throw new ApiError(msg, res.status);
  }
  if (data === null) throw new ApiError('服务返回了无法解析的数据', res.status);
  return data;
}

const errText = (err) => (err && err.message) || String(err);

/* ---------------- DOM helpers ---------------- */

function setText(node, text) {
  if (node) node.textContent = text == null ? '' : String(text);
}

function h(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === 'text') node.textContent = String(v);
    else if (k === 'className') node.className = v;
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, '');
    else node.setAttribute(k, String(v));
  }
  for (const c of [].concat(children)) {
    if (c == null) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function clear(node) {
  if (node) node.replaceChildren();
}

function alertWorkspace(message, tone = 'info') {
  if (!el.workspaceAlert) return;
  el.workspaceAlert.textContent = message || '';
  el.workspaceAlert.dataset.state = tone;
  el.workspaceAlert.hidden = !message;
}

function setStatus(node, message, tone = 'info') {
  if (!node) return;
  node.textContent = message || '';
  node.dataset.state = tone;
}

const fmtNum = (n, digits = 3) => (Number.isFinite(n) ? Number(n).toFixed(digits).replace(/\.?0+$/, '') : '—');
const fmtMm = (m) => (Number.isFinite(m) ? `${Math.round(m * 1000)}` : '—');

function sizeText(size) {
  if (!Array.isArray(size) || size.length < 3) return '—';
  return `${fmtMm(size[0])} × ${fmtMm(size[1])} × ${fmtMm(size[2])} mm`;
}

/* ---------------- Navigation ---------------- */

function navButtons() {
  return Array.from(document.querySelectorAll('[data-stage]')).filter((b) => STAGES.includes(b.dataset.stage));
}

function showStage(stage, { focus = false } = {}) {
  if (!STAGES.includes(stage)) return;
  for (const btn of navButtons()) {
    const active = btn.dataset.stage === stage;
    if (active) btn.setAttribute('aria-current', 'step');
    else btn.removeAttribute('aria-current');
    if (active && focus) btn.focus();
  }
  for (const s of STAGES) {
    const panel = $(`panel-${s}`);
    if (panel) panel.hidden = s !== stage;
  }
  if (stage === 'review') loadRuns();
}

function initNav() {
  const buttons = navButtons();
  buttons.forEach((btn, i) => {
    if (!btn.getAttribute('aria-controls')) btn.setAttribute('aria-controls', `panel-${btn.dataset.stage}`);
    btn.addEventListener('click', () => showStage(btn.dataset.stage));
    btn.addEventListener('keydown', (e) => {
      let next = null;
      if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = buttons[(i + 1) % buttons.length];
      else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = buttons[(i - 1 + buttons.length) % buttons.length];
      else if (e.key === 'Home') next = buttons[0];
      else if (e.key === 'End') next = buttons[buttons.length - 1];
      if (next) {
        e.preventDefault();
        showStage(next.dataset.stage, { focus: true });
      }
    });
  });
  showStage('generate');
}

/* ---------------- Control gating ---------------- */

function updateControls() {
  const busy = Boolean(state.job);
  const busyTrain = busy && state.job.kind === 'train';
  const sceneLocked = busy || state.scenePending;
  const hasKey = Boolean(state.config && state.config.has_key);
  const asset = selectedAsset();
  const stageOk = Boolean(state.stage) && !state.stageError;

  if (el.generateBtn) el.generateBtn.disabled = sceneLocked || !hasKey;
  if (el.prompt) el.prompt.disabled = busy && state.job.kind === 'generate';
  if (el.assetName) el.assetName.disabled = busy && state.job.kind === 'generate';

  if (el.prepareBtn) el.prepareBtn.disabled = sceneLocked || !state.workbenchReady || !asset || !asset.has_glb || !stageOk;
  if (el.widthMm) el.widthMm.disabled = sceneLocked;
  el.orientation.disabled = sceneLocked;

  const preflightOk = state.run?.status === 'prepared' && state.sceneLoaded;
  if (el.trainBtn) el.trainBtn.disabled = sceneLocked || !state.workbenchReady || !preflightOk;
  if (el.cancelBtn) el.cancelBtn.disabled = !busyTrain;
  el.collisionToggle.disabled = state.activeSceneId === null;
  for (const button of el.assetList.querySelectorAll('button')) button.disabled = sceneLocked;

  const r = state.replay;
  const replayOk = Boolean(r && r.steps > 0);
  if (el.playReplay) {
    el.playReplay.disabled = !replayOk;
    const label = r && r.playing ? '暂停' : '播放';
    setText(el.playReplay, label);
    el.playReplay.setAttribute('aria-label', `${label}回放`);
    el.playReplay.setAttribute('aria-pressed', r && r.playing ? 'true' : 'false');
  }
  if (el.seekReplay) el.seekReplay.disabled = !replayOk;
}

/* ---------------- Config ---------------- */

async function loadConfig() {
  setText(el.apiStatus, '连接中…');
  try {
    const cfg = await api('/api/config');
    state.config = cfg;
    const wb = cfg.workbench || {};
    state.workbenchReady = Boolean(wb.ready);
    setStatus(el.apiStatus, cfg.has_key ? '生成服务已配置' : '未配置生成密钥', cfg.has_key ? 'ok' : 'warn');
    setStatus(el.armStatus, wb.ready ? 'lux3D-engine · 就绪' : `lux3D-engine · 不可用${wb.reason ? `：${wb.reason}` : ''}`, wb.ready ? 'ok' : 'warn');
    if (!cfg.has_key) setStatus(el.generationStatus, '服务端未配置生成密钥，暂不能生成新模型。可直接选用已有资产。', 'warn');
  } catch (err) {
    state.config = null;
    state.workbenchReady = false;
    setStatus(el.apiStatus, '服务不可达', 'error');
    setStatus(el.armStatus, 'lux3D-engine · 状态未知', 'warn');
    alertWorkspace(`无法读取配置：${errText(err)}`, 'error');
  }
  updateControls();
}

/* ---------------- Assets ---------------- */

function selectedAsset() {
  return state.assets.find((a) => a.asset_id === state.selectedAssetId) || null;
}

async function loadAssets({ select, preview = true } = {}) {
  try {
    const data = await api('/api/assets');
    state.assets = Array.isArray(data.assets) ? data.assets : [];
  } catch (err) {
    alertWorkspace(`资产列表加载失败：${errText(err)}`, 'error');
    renderAssets();
    return false;
  }
  if (select && state.assets.some((a) => a.asset_id === select)) {
    state.selectedAssetId = null; // force reselect
    renderAssets();
    await selectAsset(select, { preview });
  } else {
    if (state.selectedAssetId && !selectedAsset()) state.selectedAssetId = null;
    renderAssets();
    renderAssetDetails();
  }
  return true;
}

function renderAssets() {
  const list = el.assetList;
  if (!list) return;
  clear(list);
  if (list.tagName === 'SELECT') {
    list.append(h('option', { value: '', text: state.assets.length ? '选择资产' : '暂无资产' }));
    for (const a of state.assets) {
      const opt = h('option', { value: a.asset_id, text: a.has_glb ? a.name : `${a.name}（无模型文件）` });
      if (a.asset_id === state.selectedAssetId) opt.selected = true;
      list.append(opt);
    }
    return;
  }
  if (!state.assets.length) {
    list.append(h('li', { className: 'empty', text: '暂无资产。先在「生成」中描述一个物体。' }));
    return;
  }
  for (const a of state.assets) {
    const active = a.asset_id === state.selectedAssetId;
    const btn = h('button', {
      type: 'button',
      className: 'asset-item',
      'aria-pressed': active ? 'true' : 'false',
      dataset: { assetId: a.asset_id },
      onclick: () => selectAsset(a.asset_id),
    }, [
      h('span', { className: 'asset-name', text: a.name }),
      h('span', { className: 'asset-meta', text: a.has_glb ? sizeText(a.bbox_m && a.bbox_m.size) : '无模型文件' }),
    ]);
    if (active) btn.setAttribute('aria-current', 'true');
    list.append(h('li', {}, btn));
  }
}

function renderAssetDetails() {
  const box = el.assetDetails;
  if (!box) return;
  clear(box);
  const a = selectedAsset();
  if (!a) {
    box.append(h('p', { className: 'muted', text: '未选择资产。' }));
    return;
  }
  const rows = [
    ['名称', a.name],
    ['资产 ID', a.asset_id],
    ['包围盒', sizeText(a.bbox_m && a.bbox_m.size)],
    ['三角面', Number.isFinite(a.triangles) ? a.triangles.toLocaleString('zh-CN') : '—'],
  ];
  if (state.run && state.run.scene) {
    const s = state.run.scene;
    rows.push(['缩放', fmtNum(s.scale, 4)]);
    rows.push(['场景尺寸', sizeText(s.size_m)]);
  }
  const dl = h('dl');
  for (const [k, v] of rows) dl.append(h('dt', { text: k }), h('dd', { text: v }));
  box.append(dl);
}

async function selectAsset(assetId, { preview = true } = {}) {
  if (assetId === state.selectedAssetId) return;
  stopPlayback();
  state.selectedAssetId = assetId || null;
  state.run = null;
  state.sceneLoaded = false;
  state.sceneToken++;
  state.reviewToken++;
  const token = ++state.assetToken;
  renderAssets();
  renderAssetDetails();
  setStatus(el.sceneStatus, '', 'info');
  updateControls();
  if (!preview) return;

  const a = selectedAsset();
  if (!a) {
    showEmpty('选择或生成一个资产以预览');
    return;
  }
  if (!a.has_glb) {
    showEmpty('该资产没有可预览的模型文件');
    return;
  }
  if (!state.stage) {
    showEmpty(state.stageError ? '渲染器不可用' : '渲染器加载中…');
    return;
  }
  setText(el.viewLabel, `${a.name} · 加载中`);
  try {
    await state.stage.asset(`/api/asset/${encodeURIComponent(a.asset_id)}?kind=glb`);
    if (token !== state.assetToken) return; // superseded
    state.activeSceneId = null;
    hideEmpty();
    state.stage.fit();
    setText(el.viewLabel, `${a.name} · 资产预览`);
  } catch (err) {
    if (token !== state.assetToken) return;
    showEmpty('模型加载失败');
    setText(el.viewLabel, a.name);
    alertWorkspace(`模型加载失败：${errText(err)}`, 'error');
  }
  updateControls();
}

function showEmpty(message) {
  if (!el.viewportEmpty) return;
  el.viewportEmpty.hidden = false;
  setText(el.viewportEmpty, message);
}
function hideEmpty() {
  if (el.viewportEmpty) el.viewportEmpty.hidden = true;
}

/* ---------------- Jobs ---------------- */

function startJob(kind, id, onUpdate, onDone) {
  state.job = { kind, id, timer: 0, failures: 0, onUpdate, onDone };
  updateControls();
  pollJob();
}

async function pollJob() {
  const job = state.job;
  if (!job) return;
  let data;
  try {
    data = await api(`/api/jobs/${encodeURIComponent(job.id)}`);
  } catch (err) {
    if (state.job !== job) return;
    if (err.status === 404) {
      finishJob(job, null, '任务已不存在');
      return;
    }
    job.failures++;
    const delay = Math.min(POLL_MS * 2 ** job.failures, POLL_MAX_BACKOFF_MS);
    alertWorkspace(`任务状态获取失败，${Math.round(delay / 1000)} 秒后重试：${errText(err)}`, 'warn');
    job.timer = setTimeout(pollJob, delay);
    return;
  }
  if (state.job !== job) return;
  if (job.failures) {
    job.failures = 0;
    alertWorkspace('');
  }
  const j = data.job || {};
  job.onUpdate(j);
  if (isTerminal(j.state)) finishJob(job, j);
  else job.timer = setTimeout(pollJob, POLL_MS);
}

function finishJob(job, j, missingMessage) {
  clearTimeout(job.timer);
  if (state.job === job) state.job = null;
  job.onDone(j, missingMessage);
  updateControls();
}

async function cancelJob() {
  const job = state.job;
  if (!job) return;
  if (el.cancelBtn) el.cancelBtn.disabled = true;
  try {
    await api(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, { method: 'POST', body: {} });
    appendLog('已请求取消');
  } catch (err) {
    alertWorkspace(`取消失败：${errText(err)}`, 'error');
    updateControls();
  }
}

/* ---------------- Generate ---------------- */

async function onGenerate(e) {
  e.preventDefault();
  if (state.job || state.scenePending) return;
  const prompt = (el.prompt && el.prompt.value || '').trim();
  const name = (el.assetName?.value || '').trim() || `block-${Date.now().toString(36)}`;
  if (!/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}$/.test(name)) {
    setStatus(el.generationStatus, '名称可用英文字母、数字、短横线和下划线。', 'warn');
    el.assetName?.focus();
    return;
  }
  if (!prompt) {
    setStatus(el.generationStatus, '请描述要生成的物体。', 'warn');
    if (el.prompt) el.prompt.focus();
    return;
  }
  if (el.generateBtn) el.generateBtn.disabled = true;
  setStatus(el.generationStatus, '提交中…', 'info');
  let res;
  try {
    res = await api('/api/generate', {
      method: 'POST',
      body: { prompt, name, version: 'G1-Turbo', face_count: 10000, format: 'glb', pbr: true, prepare_scene: true },
    });
  } catch (err) {
    setStatus(el.generationStatus, `提交失败：${errText(err)}`, 'error');
    updateControls();
    return;
  }
  const id = res.id || (res.job && res.job.id);
  if (!id) {
    setStatus(el.generationStatus, '服务未返回任务 ID', 'error');
    updateControls();
    return;
  }
  setStatus(el.generationStatus, '已提交，等待生成…', 'info');
  trackGeneration(id);
}

function trackGeneration(id) {
  startJob('generate', id, (j) => {
    const label = {
      queued: '等待提交', init: '已发起生成', running: '正在生成', success: '正在载入模型',
      preparing: '模型已生成，正在校验碰撞场景', prepared: '仿真场景已准备',
    }[j.status_name] || '处理中';
    setStatus(el.generationStatus, j.message ? `${label}：${j.message}` : `${label}…`, 'info');
  }, async (j, missing) => {
    if (!j) {
      setStatus(el.generationStatus, missing || '生成任务中断', 'error');
      return;
    }
    if (j.asset && (TERMINAL_OK.has(j.state) || j.status_name === 'preparing')) {
      await adoptGeneratedAsset(j);
    } else if (TERMINAL_CANCEL.has(j.state)) {
      setStatus(el.generationStatus, '已取消生成。', 'warn');
    } else {
      setStatus(el.generationStatus, `生成失败${j.message ? `：${j.message}` : ''}`, 'error');
    }
  });
}

/* ---------------- Scene ---------------- */

async function adoptGeneratedAsset(job) {
  const assetId = job.asset.asset_id;
  state.scenePending = true;
  updateControls();
  try {
    const run = job.run_id ? (await api(`/api/runs/${encodeURIComponent(job.run_id)}`)).run : null;
    if (run && run.status !== 'prepared') return;
    const ok = await loadAssets({ select: assetId, preview: !run });
    if (!ok || state.selectedAssetId !== assetId) throw new Error('资产列表中找不到新模型');
    showStage('scene');
    if (run) {
      if (!(await activatePreparedScene(run, state.sceneToken))) return;
      setStatus(el.generationStatus, '生成完成，已导入仿真。', 'ok');
      setStatus(el.sceneStatus, '场景已自动准备。可调整参数，或进入训练。', 'ok');
    } else if (job.status_name === 'preparing') {
      setStatus(el.generationStatus, '模型已生成，自动准备场景失败。', 'warn');
      setStatus(el.sceneStatus, `自动准备失败：${job.message}`, 'error');
    } else {
      setStatus(el.generationStatus, '生成完成。', 'ok');
      setStatus(el.sceneStatus, '可设置夹取参数并准备场景。', 'info');
    }
  } catch (error) {
    setStatus(el.generationStatus, `载入生成结果失败：${errText(error)}`, 'error');
    setStatus(el.sceneStatus, `载入场景失败：${errText(error)}`, 'error');
  } finally {
    state.scenePending = false;
    updateControls();
  }
}

async function activatePreparedScene(run, token) {
  if (!run?.scene) throw new Error('服务未返回场景数据');
  if (!state.stage) throw new Error('三维视图不可用');
  await state.stage.scene(run.scene);
  if (token !== state.sceneToken) return false;
  state.run = run;
  state.activeSceneId = run.id;
  state.sceneLoaded = true;
  el.widthMm.value = run.scene.width_mm;
  el.orientation.value = run.scene.orientation;
  hideEmpty();
  state.stage.fit();
  state.stage.collision(Boolean(el.collisionToggle && el.collisionToggle.checked));
  setText(el.viewLabel, `${run.asset.name} · 仿真场景`);
  renderAssetDetails();
  resetTrainingView();
  return true;
}

function readWidth() {
  const v = Number(el.widthMm && el.widthMm.value);
  if (!Number.isFinite(v) || v < 45 || v > 60) return null;
  return v;
}

async function onPrepare(e) {
  e.preventDefault();
  if (state.job || state.scenePending) return;
  const a = selectedAsset();
  if (!a) {
    setStatus(el.sceneStatus, '请先选择资产。', 'warn');
    return;
  }
  const width = readWidth();
  if (width == null) {
    setStatus(el.sceneStatus, '请输入 45 至 60 毫米的夹取宽度。', 'warn');
    if (el.widthMm) el.widthMm.focus();
    return;
  }
  stopPlayback();
  const token = ++state.sceneToken;
  state.assetToken++;
  state.reviewToken++;
  state.run = null;
  state.sceneLoaded = false;
  state.scenePending = true;
  updateControls();
  setStatus(el.sceneStatus, '正在搭建场景…', 'info');
  try {
    const res = await api('/api/workbench/scene', {
      method: 'POST',
      body: { asset: a.name, asset_id: a.asset_id, width_mm: width, orientation: el.orientation.value },
    });
    if (token !== state.sceneToken) return;
    if (!(await activatePreparedScene(res.run, token))) return;
    setStatus(el.sceneStatus, `场景校验通过，夹取宽度 ${width} 毫米，可以开始训练。`, 'ok');
    showStage('train');
  } catch (err) {
    if (token !== state.sceneToken) return;
    state.sceneLoaded = false;
    setStatus(el.sceneStatus, `场景准备失败：${errText(err)}`, 'error');
  } finally {
    state.scenePending = false;
    updateControls();
  }
}

/* ---------------- Training ---------------- */

function resetTrainingView() {
  state.lossPoints = [];
  state.logLines = [];
  clear(el.jobLog);
  setText(el.trainingStage, '未开始');
  setProgress(null);
  drawLossChart();
}

function setProgress(p) {
  const node = el.trainingProgress;
  if (!node) return;
  const total = p && Number(p.total);
  const done = p && Number(p.completed);
  const known = Number.isFinite(total) && total > 0 && Number.isFinite(done);
  if (node.tagName === 'PROGRESS') {
    if (known) {
      node.max = total;
      node.value = Math.min(done, total);
    } else {
      node.removeAttribute('value');
    }
  }
  const parts = [];
  if (known) parts.push(`${done} / ${total}`);
  if (p && Number.isFinite(p.epoch) && Number.isFinite(p.epochs)) parts.push(`轮次 ${p.epoch} / ${p.epochs}`);
  if (p && Number.isFinite(p.successes)) parts.push(`成功 ${p.successes}`);
  const label = parts.join(' · ');
  node.setAttribute('aria-valuetext', label || '进度未知');
  if (node.tagName !== 'PROGRESS') setText(node, label);
}

function appendLog(line) {
  if (!line) return;
  state.logLines.push(String(line));
  if (state.logLines.length > LOG_LIMIT) state.logLines.splice(0, state.logLines.length - LOG_LIMIT);
  renderLog();
}

function syncLog(log) {
  let lines;
  if (Array.isArray(log)) lines = log.map(String);
  else if (typeof log === 'string') lines = log.split('\n').filter(Boolean);
  else return;
  state.logLines = lines.slice(-LOG_LIMIT);
  renderLog();
}

function renderLog() {
  const box = el.jobLog;
  if (!box) return;
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
  clear(box);
  const frag = document.createDocumentFragment();
  for (const line of state.logLines) frag.append(h('div', { className: 'log-line', text: line }));
  box.append(frag);
  if (nearBottom) box.scrollTop = box.scrollHeight;
}

function recordLoss(p) {
  if (!p || !Number.isFinite(p.epoch)) return;
  const tl = Number(p.training_loss);
  const vl = Number(p.validation_loss);
  if (!Number.isFinite(tl) && !Number.isFinite(vl)) return;
  const last = state.lossPoints[state.lossPoints.length - 1];
  const pt = { epoch: p.epoch, training_loss: Number.isFinite(tl) ? tl : null, validation_loss: Number.isFinite(vl) ? vl : null };
  if (last && last.epoch === p.epoch) state.lossPoints[state.lossPoints.length - 1] = pt;
  else state.lossPoints.push(pt);
  drawLossChart();
}

function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}


function drawLossChart(points = state.lossPoints) {
  const cv = el.lossChart;
  if (!cv || !cv.getContext) return;
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth || 320;
  const ht = cv.clientHeight || 160;
  cv.width = Math.round(w * dpr);
  cv.height = Math.round(ht * dpr);
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, ht);
  const muted = cssVar('--muted', '#8a8f98');
  const pad = { l: 40, r: 12, t: 12, b: 22 };
  ctx.font = '11px system-ui, sans-serif';
  ctx.fillStyle = muted;
  if (!points || !points.length) {
    ctx.textAlign = 'center';
    ctx.fillText('等待训练数据', w / 2, ht / 2);
    cv.setAttribute('aria-label', '损失曲线：暂无数据');
    return;
  }
  const vals = points.flatMap((p) => [p.training_loss, p.validation_loss]).filter(Number.isFinite);
  if (!vals.length) return;
  let lo = Math.min(...vals);
  let hi = Math.max(...vals);
  if (hi - lo < 1e-9) hi = lo + 1e-9;
  const lossLabel = value => Math.abs(value) > 0 && Math.abs(value) < 0.001 ? value.toExponential(1) : fmtNum(value);
  const e0 = points[0].epoch;
  const e1 = Math.max(points[points.length - 1].epoch, e0 + 1);
  const x = (e) => pad.l + ((e - e0) / (e1 - e0)) * (w - pad.l - pad.r);
  const y = (v) => pad.t + (1 - (v - lo) / (hi - lo)) * (ht - pad.t - pad.b);
  ctx.strokeStyle = cssVar('--grid', 'rgba(128,128,128,.2)');
  ctx.lineWidth = 1;
  ctx.textAlign = 'right';
  for (let i = 0; i <= 3; i++) {
    const v = lo + ((hi - lo) * i) / 3;
    ctx.beginPath();
    ctx.moveTo(pad.l, y(v));
    ctx.lineTo(w - pad.r, y(v));
    ctx.stroke();
    ctx.fillText(lossLabel(v), pad.l - 6, y(v) + 4);
  }
  ctx.textAlign = 'center';
  ctx.fillText(`轮次 ${e0}–${points[points.length - 1].epoch}`, (pad.l + w - pad.r) / 2, ht - 6);
  const series = [
    ['training_loss', cssVar('--accent', '#4f7cff')],
    ['validation_loss', cssVar('--warn', '#e8b36a')],
  ];
  for (const [key, color] of series) {
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    let started = false;
    for (const p of points) {
      if (!Number.isFinite(p[key])) continue;
      if (started) ctx.lineTo(x(p.epoch), y(p[key]));
      else { ctx.moveTo(x(p.epoch), y(p[key])); started = true; }
    }
    ctx.stroke();
  }
  const last = points[points.length - 1];
  cv.setAttribute('aria-label', `损失曲线：轮次 ${last.epoch}，训练损失 ${lossLabel(last.training_loss)}，验证损失 ${lossLabel(last.validation_loss)}`);
}

function trackTraining(id) {
  startJob('train', id, job => {
    if (!isTerminal(job.state)) setText(el.trainingStage, TRAIN_PHASE[job.status_name] || '正在准备');
    const progress = job.progress;
    if (progress?.epochs) {
      setProgress({ ...progress, completed: progress.epoch, total: progress.epochs });
    } else {
      setProgress(progress);
    }
    syncLog(job.log);
    recordLoss(progress);
  }, async (job, missing) => {
    if (job?.result?.id === state.run?.id) state.run = job.result;
    if (TERMINAL_OK.has(job?.state) && job.result?.result) {
      const result = job.result.result;
      state.lossPoints = result.curve;
      drawLossChart();
      setText(el.trainingStage, `评估完成 · ${result.successes}/${result.episodes} 集成功`);
      showStage('review');
      await selectRun(job.result.id);
    } else {
      const cancelled = job?.status_name === 'cancelled';
      const message = job?.message || missing || '训练中断';
      setText(el.trainingStage, message);
      alertWorkspace(cancelled ? message : `${message} 可重新确认场景后再开始训练。`, cancelled ? 'warn' : 'error');
    }
    updateControls();
  });
}

async function onTrain() {
  if (state.job || state.scenePending || state.run?.status !== 'prepared' || !state.sceneLoaded) return;
  el.trainBtn.disabled = true;
  alertWorkspace('');
  resetTrainingView();
  setText(el.trainingStage, '正在提交训练');
  try {
    const run = state.run;
    if (state.activeSceneId !== run.id) await state.stage.scene(run.scene);
    if (state.run !== run) return;
    state.activeSceneId = run.id;
    const response = await api('/api/workbench/train', { method: 'POST', body: { run_id: run.id } });
    run.status = 'running';
    trackTraining(response.job.id);
  } catch (error) {
    setText(el.trainingStage, '无法启动训练');
    alertWorkspace(errText(error), 'error');
    updateControls();
  }
}

async function loadRuns() {
  try {
    const response = await api('/api/runs');
    state.runs = response.runs.filter(run => run.kind === 'policy' && run.status !== 'prepared'
      && (!run.result || run.result.evaluation_backend === 'oriera-wasm'));
    renderRuns();
  } catch (error) {
    alertWorkspace(`训练记录读取失败：${errText(error)}`, 'error');
  }
}

function renderRuns() {
  clear(el.runList);
  if (!state.runs.length) {
    el.runList.append(h('li', { className: 'list-empty', text: '暂无训练记录' }));
    return;
  }
  const labels = { failed: '失败', cancelled: '已取消', running: '进行中', done: '已评估' };
  for (const run of state.runs) {
    const result = run.result;
    const label = result ? `${result.successes}/${result.episodes} 集` : labels[run.status] || run.status;
    const title = `${run.asset.name} · ${new Date(run.created_at).toLocaleTimeString('zh-CN')}`;
    const button = h('button', {
      type: 'button', className: 'item-btn', 'aria-pressed': run.id === state.reviewRun?.id ? 'true' : 'false',
      onclick: () => selectRun(run.id),
    }, [h('span', { text: title }), h('small', { text: label })]);
    el.runList.append(h('li', {}, button));
  }
}

async function selectRun(id) {
  if (state.scenePending) return;
  stopPlayback();
  const ticket = ++state.reviewToken;
  state.reviewRun = null;
  clear(el.episodeList);
  setText(el.evaluationSummary, '正在读取训练记录');
  try {
    const { run } = await api(`/api/runs/${encodeURIComponent(id)}`);
    if (ticket !== state.reviewToken) return;
    state.reviewRun = run;
    renderRuns();
    if (!run.result) {
      setText(el.evaluationSummary, run.error || '这次训练暂无独立评估结果。');
      return;
    }
    if (run.result.evaluation_backend !== 'oriera-wasm') {
      setText(el.evaluationSummary, '此历史记录使用旧版评估方式。请在工作台重新训练后回放。');
      return;
    }
    const { result } = run;
    clear(el.evaluationSummary);
    const score = h('strong', { className: 'result-score', text: `${result.successes} / ${result.episodes}` });
    const status = h('span', {
      className: 'result-verdict', 'data-result': result.passed ? 'success' : 'fail',
      text: result.passed ? '达到抓放验收门槛' : '未达到抓放验收门槛',
    });
    el.evaluationSummary.append(score, status);
    if (Number.isInteger(result.collision_free_episodes)) {
      el.evaluationSummary.append(h('p', {
        text: `原始物理回放中，${result.collision_free_episodes}/${result.episodes} 集无穿透。`,
      }));
    }
    for (const [index, row] of result.runs.entries()) {
      const button = h('button', {
        type: 'button', className: 'item-btn', dataset: { seed: String(row.seed) }, 'aria-pressed': 'false',
        onclick: () => loadReplay(row, run),
      }, [
        h('span', { text: `第 ${index + 1} 集` }),
        h('small', { 'data-result': row.success ? 'success' : 'fail', text: `${row.success ? '抓放成功' : '未完成'} · ${row.steps} 步` }),
      ]);
      el.episodeList.append(h('li', {}, button));
    }
    await state.stage.scene(run.scene);
    if (ticket !== state.reviewToken) return;
    state.activeSceneId = run.id;
    hideEmpty();
    setText(el.viewLabel, `${run.asset.name} · 评估场景`);
    updateControls();
  } catch (error) {
    if (ticket !== state.reviewToken) return;
    setText(el.evaluationSummary, '无法加载评估记录');
    alertWorkspace(errText(error), 'error');
  }
}

async function loadReplay(row, run) {
  if (state.scenePending) return;
  stopPlayback();
  const ticket = ++state.reviewToken;
  const buttons = el.episodeList.querySelectorAll('button');
  for (const button of buttons) {
    button.setAttribute('aria-pressed', String(button.dataset.seed === String(row.seed)));
  }
  alertWorkspace('');
  setText(el.replayLabel, '正在核对仿真轨迹');
  try {
    const result = await api(`/api/workbench/runs/${encodeURIComponent(run.id)}/replay/${row.seed}`);
    if (ticket !== state.reviewToken) return;
    if (state.activeSceneId !== run.id) await state.stage.scene(run.scene);
    if (ticket !== state.reviewToken) return;
    state.activeSceneId = run.id;
    const steps = await state.stage.replay(result.replay);
    if (ticket !== state.reviewToken) return;
    state.replay = { seed: row.seed, steps, frame: 0, playing: false, rafId: 0 };
    el.seekReplay.max = steps;
    showFrame(0);
    hideEmpty();
    setText(el.viewLabel, `${run.asset.name} · 已核对轨迹`);
    updateControls();
  } catch (error) {
    if (ticket !== state.reviewToken) return;
    setText(el.replayLabel, '回放不可用');
    alertWorkspace(errText(error), 'error');
  }
}

function showFrame(frame) {
  const replay = state.replay;
  if (!replay) return;
  replay.frame = Math.min(replay.steps, Math.max(0, frame));
  state.stage.frame(replay.frame);
  el.seekReplay.value = replay.frame;
  el.seekReplay.style.setProperty('--seek', `${replay.steps ? (100 * replay.frame / replay.steps) : 0}%`);
  const time = (replay.frame / STEPS_PER_SECOND).toFixed(2);
  const end = (replay.steps / STEPS_PER_SECOND).toFixed(2);
  setText(el.replayLabel, `${time} / ${end} 秒`);
}

function pausePlayback() {
  const replay = state.replay;
  if (!replay) return;
  cancelAnimationFrame(replay.rafId);
  replay.playing = false;
  updateControls();
}

function stopPlayback() {
  pausePlayback();
  state.replay = null;
  el.seekReplay.value = 0;
  el.seekReplay.style.setProperty('--seek', '0%');
  setText(el.replayLabel, '选择一段回放');
  updateControls();
}

function playOrPause() {
  const replay = state.replay;
  if (!replay || replay.playing) {
    pausePlayback();
    return;
  }
  if (replay.frame === replay.steps) showFrame(0);
  const from = replay.frame;
  let start;
  replay.playing = true;
  updateControls();
  const tick = time => {
    if (state.replay !== replay || !replay.playing) return;
    start ??= time;
    const next = Math.min(replay.steps, from + Math.floor((time - start) * STEPS_PER_SECOND / 1000));
    if (next !== replay.frame) showFrame(next);
    if (next === replay.steps) pausePlayback();
    else replay.rafId = requestAnimationFrame(tick);
  };
  replay.rafId = requestAnimationFrame(tick);
}

async function init() {
  try {
    state.stage = await createWorkbenchStage(el.viewport);
  } catch (error) {
    state.stageError = error;
    alertWorkspace('无法启动三维视图。请检查浏览器的 WebGL 支持。', 'error');
  }
  initNav();
  el.generateForm.addEventListener('submit', onGenerate);
  el.sceneForm.addEventListener('submit', onPrepare);
  const invalidateScene = () => {
    if (!state.run || state.run.status !== 'prepared') return;
    state.sceneToken++;
    state.run = null;
    state.sceneLoaded = false;
    setStatus(el.sceneStatus, '物体设置已修改，请重新确认场景。', 'warn');
    renderAssetDetails();
    updateControls();
  };
  el.widthMm.addEventListener('input', invalidateScene);
  el.orientation.addEventListener('change', invalidateScene);
  el.trainBtn.addEventListener('click', onTrain);
  el.cancelBtn.addEventListener('click', cancelJob);
  el.playReplay.addEventListener('click', playOrPause);
  el.seekReplay.addEventListener('input', () => {
    pausePlayback();
    showFrame(el.seekReplay.valueAsNumber);
  });
  el.fitView.addEventListener('click', () => state.stage?.fit());
  el.collisionToggle.addEventListener('change', () => state.stage?.collision(el.collisionToggle.checked));
  resetTrainingView();
  updateControls();
  await Promise.all([loadConfig(), loadAssets()]);
  const requestedRun = new URLSearchParams(location.search).get('run');
  if (requestedRun) {
    showStage('review');
    await selectRun(requestedRun);
    return;
  }
  try {
    const response = await api('/api/jobs');
    const running = response.jobs.find(job => job.kind === 'policy' && ['queued', 'running'].includes(job.state));
    if (running && !state.job) {
      showStage('train');
      trackTraining(running.id);
      return;
    }
    const generating = response.jobs.find(job => job.kind === 'generate' && !job.dry_run
      && job.payload?.prepare_scene !== false && (job.payload?.format || 'glb') === 'glb'
      && ['queued', 'running'].includes(job.state));
    if (generating && !state.job) {
      trackGeneration(generating.id);
      return;
    }
    const generated = response.jobs.find(job => job.kind === 'generate' && job.run_id && job.asset);
    if (generated && !state.job) await adoptGeneratedAsset(generated);
  } catch (error) {
    alertWorkspace(`无法读取正在运行的任务：${errText(error)}`, 'warn');
  }
}

init();
