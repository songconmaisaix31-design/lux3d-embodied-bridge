// Thin client for the local bridge API (src/lux3d_bridge/server.py).
// Every helper throws on a non-2xx response and surfaces the server's message.

async function request(path, options = {}) {
  const response = await fetch(path, options);
  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok || (body && body.ok === false)) {
    const message = (body && body.error) || `${response.status} ${response.statusText}`;
    throw new Error(message);
  }
  return body;
}

export const api = {
  config: () => request('/api/config'),
  robot: (path) => request('/api/robot' + (path ? `?path=${encodeURIComponent(path)}` : '')),
  assets: () => request('/api/assets'),
  generate: (payload) =>
    request('/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  prepare: (payload) =>
    request('/api/prepare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  train: (payload) =>
    request('/api/train', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  cancel: (jobId) =>
    request(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' }),
  runs: () => request('/api/runs'),
  run: (id) => request(`/api/runs/${encodeURIComponent(id)}`),
  job: (id) => request(`/api/jobs/${id}`),
  assetUrl: (name, kind) => `/api/asset/${encodeURIComponent(name)}?kind=${kind}`,
};

export async function fetchArrayBuffer(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`GET ${url} -> ${response.status}`);
  return response.arrayBuffer();
}
