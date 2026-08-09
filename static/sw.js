// secondtrack service worker.
//
// Deliberately narrow: only the static shell is cached. Pages are behind a
// login and full of customer data, so caching them would leave that data on the
// device for whoever picks the tablet up next — an offline copy is not worth
// that. Navigations go to the network and fall back to a small offline notice.

const VERSION = 'st-__ASSET_V__';
const SHELL = [
  '/static/style.css',
  '/static/app.js',
  '/static/warehouse.js',
  '/static/fonts/fredoka-latin.woff2',
  '/static/fonts/fredoka-latin-ext.woff2',
  '/static/icon-192.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(VERSION)
      // Individually, so one 404 cannot fail the whole install.
      .then((c) => Promise.all(SHELL.map((u) => c.add(u).catch(() => {}))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Static assets: cache first. They are versioned by ?v=<mtime>, so a deploy
  // produces new URLs and the old entries fall out with the next VERSION bump.
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(VERSION).then((c) => c.put(req, copy));
        }
        return res;
      }))
    );
    return;
  }

  // Uploads are user data: fetch them, never keep them.
  if (url.pathname.startsWith('/uploads/')) return;

  // Everything else is a page — network only, with a notice when offline.
  if (req.mode === 'navigate') {
    e.respondWith(fetch(req).catch(() => new Response(
      '<!doctype html><meta charset="utf-8">' +
      '<meta name="viewport" content="width=device-width,initial-scale=1">' +
      '<title>Offline · secondtrack</title>' +
      '<style>body{font-family:system-ui,sans-serif;background:#201019;color:#fff;' +
      'display:grid;place-items:center;height:100vh;margin:0;text-align:center;padding:2rem}' +
      'button{font:inherit;padding:.6rem 1rem;border-radius:10px;border:1px solid #444;' +
      'background:#351621;color:#fff;margin-top:1rem}</style>' +
      '<div><h1>Offline</h1><p>secondtrack braucht eine Verbindung zum Server.</p>' +
      '<button onclick="location.reload()">Erneut versuchen</button></div>',
      { headers: { 'Content-Type': 'text/html; charset=utf-8' }, status: 503 }
    )));
  }
});
