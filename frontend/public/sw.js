// GlucoPilot service worker. It exists so the installed app opens on its own
// (Android only treats a site as an app when one is registered) and so a
// launch with no network shows a message instead of the browser's error page.
// It caches nothing: pages, assets and health data always come from the server.

const OFFLINE_PAGE = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0f8a71">
<title>GlucoPilot — offline</title>
<style>
  body{font-family:system-ui,sans-serif;background:#f6f8f9;color:#172033;margin:0;display:grid;place-items:center;min-height:100vh;text-align:center;padding:24px;box-sizing:border-box}
  .logo{width:48px;height:48px;border-radius:12px;background:#0f8a71;color:#fff;display:grid;place-items:center;font:800 18px monospace;margin:0 auto 16px}
  p{color:#475569;font-size:15px;line-height:1.45;margin:6px 0 20px}
  button{background:#0f8a71;color:#fff;border:0;border-radius:8px;padding:10px 20px;font-weight:600;font-size:15px}
</style></head><body><main>
  <div class="logo">GP</div>
  <h1 style="font-size:20px;margin:0">No connection</h1>
  <p>GlucoPilot needs the network to load your data.</p>
  <button onclick="location.reload()">Try again</button>
</main></body></html>`;

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

self.addEventListener('fetch', (event) => {
  // Only page loads; API calls and assets go straight to the network untouched.
  if (event.request.mode !== 'navigate' || event.request.method !== 'GET') return;
  event.respondWith(
    fetch(event.request).catch(
      () => new Response(OFFLINE_PAGE, { headers: { 'Content-Type': 'text/html; charset=utf-8' } }),
    ),
  );
});
