// Shared helpers for SpendPilot AI.
document.addEventListener('DOMContentLoaded', () => {
  if (window.lucide) lucide.createIcons();
});

const csrfToken = () => document.querySelector('meta[name="csrf-token"]').content;

async function api(path, options = {}) {
  const opts = Object.assign({ headers: {} }, options);
  opts.headers['X-CSRF-Token'] = csrfToken();
  if (opts.json) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.json);
    opts.method = opts.method || 'POST';
  }
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch (_) { /* non-JSON response */ }
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

function money(v, cur = 'USD') {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: cur }).format(v || 0);
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
