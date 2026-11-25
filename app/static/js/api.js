const json = async (url, options) => {
  const response = await fetch(url, options);
  let body = {};
  try { body = await response.json(); } catch {}
  if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`);
  return body;
};
export const api = {
  workers: () => json('/api/workers'),
  scheduler: () => json('/api/scheduler/status'),
  decisions: () => json('/api/scheduler/decisions?limit=100'),
  job: id => json(`/api/jobs/${encodeURIComponent(id)}`),
  analysis: id => json(`/api/jobs/${encodeURIComponent(id)}/analysis`),
  metrics: id => json(`/api/jobs/${encodeURIComponent(id)}/metrics`),
  mode: mode => json('/api/scheduler/mode', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})}),
  upload: form => json('/api/upload', {method:'POST',body:form}),
  cancelJob: id => json(`/api/jobs/${encodeURIComponent(id)}/cancel`, {method:'POST'}),
};
