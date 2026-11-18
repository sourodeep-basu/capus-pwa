// Campus Life service worker: offline shell, push (with action buttons), background sync.
const CACHE = 'campus-life-v8';
const SHELL = ['/', '/manifest.json', '/icon-192.png', '/icon-512.png'];

// --- tiny IndexedDB helpers (same database the app uses) ---
const idb = () => new Promise((res, rej) => {
  const r = indexedDB.open('campus-life', 1);
  r.onupgradeneeded = () => r.result.createObjectStore('kv');
  r.onsuccess = () => res(r.result);
  r.onerror = () => rej(r.error);
});
const idbGet = async k => { const d = await idb(); return new Promise((res, rej) => { const q = d.transaction('kv').objectStore('kv').get(k); q.onsuccess = () => res(q.result); q.onerror = () => rej(q.error); }); };
const idbSet = async (k, v) => { const d = await idb(); return new Promise((res, rej) => { const t = d.transaction('kv', 'readwrite'); t.objectStore('kv').put(v, k); t.oncomplete = res; t.onerror = () => rej(t.error); }); };

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});

// Network-first so updates show up; the cache is the offline fallback. API calls are never cached.
self.addEventListener('fetch', e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== 'GET' || url.pathname.startsWith('/api/')) return;
  e.respondWith(fetch(req).then(res => {
    if (res && (res.ok || res.type === 'opaque')) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(req, copy)); }
    return res;
  }).catch(() => caches.match(req).then(r => r || caches.match('/'))));
});

// --- background sync: push the latest local snapshot when the connection returns ---
async function bgSync() {
  const auth = await idbGet('auth'), st = await idbGet('state');
  if (!auth || !st) return;
  const { ntf, ...state } = st;
  const r = await fetch('/api/data/push', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + auth.token },
    body: JSON.stringify({ ts: st._ts || Date.now(), state })
  });
  if (!r.ok && r.status !== 401) throw new Error('retry later');
}
self.addEventListener('sync', e => { if (e.tag === 'cl-sync') e.waitUntil(bgSync()); });

// --- push notifications ---
self.addEventListener('push', e => {
  let d = { title: 'Campus Life', body: 'You have a reminder.' };
  try { if (e.data) d = Object.assign(d, e.data.json()); } catch (_) {}
  e.waitUntil(self.registration.showNotification(d.title, {
    body: d.body, icon: '/icon-192.png', badge: '/icon-192.png',
    tag: d.tag || undefined, actions: d.actions || [],
    data: { url: d.url || '/', task_id: d.task_id || null }
  }));
});

self.addEventListener('notificationclick', e => {
  const n = e.notification, d = n.data || {};
  n.close();
  if (e.action === 'snooze') {
    e.waitUntil((async () => {
      const st = await idbGet('state'), did = st && st.ntf && st.ntf.did;
      if (did) await fetch('/api/snooze', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_id: did, title: n.title, body: n.body, minutes: 10 }) });
    })());
    return;
  }
  if (e.action === 'done' && d.task_id) {
    // Queue the action; the app applies it next time it is open (or right away if it is).
    e.waitUntil((async () => {
      const c = (await idbGet('cmds')) || [];
      c.push({ k: 'done', id: d.task_id });
      await idbSet('cmds', c);
      (await clients.matchAll({ type: 'window' })).forEach(w => w.postMessage({ type: 'cmds' }));
    })());
    return;
  }
  e.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
    for (const c of list) if ('focus' in c) return c.focus();
    return clients.openWindow(d.url || '/');
  }));
});
