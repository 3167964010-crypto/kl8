// 离线缓存：在线时强制走网络（绕过 HTTP 缓存），断网时回退到最后一次缓存
// 这样手机断网仍能打开页面，在线时一定拿到最新版本与最新开奖

// 缓存版本：改动页面或数据口径时递增，旧的缓存会在 activate 时被清掉
const CACHE = 'kl8-cache-v3';

// 首次安装时预缓存核心页面与数据快照
const CORE = [
  './',
  './index.html',
  './algos.html',
  './prize.html',
  './test_algos.html',
  './snapshot.json',
  './algos_snapshot.json',
  './kl8_snapshot.json',
  './manifest.json',
  './gate.js'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      // 逐个缓存而不是 addAll：addAll 是"全成功才成功"，
      // 一旦某个文件 404，新 SW 就永远装不上，老 SW 会一直把旧页面喂给用户。
      .then((cache) => Promise.all(
        CORE.map((url) => cache.add(url).catch(() => {}))
      ))
      .then(() => self.skipWaiting())
  );
});

// 页面可主动通知 SW 立刻接管，避免更新要等下一次打开才生效
self.addEventListener('message', (event) => {
  if (event.data === 'SKIP_WAITING') self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
      // 新版本接管后，把所有已打开的页面强制刷新一次，
      // 避免用户还盯着旧页面看（旧页面的 JS 与文案不会自己变）
      .then(() => self.clients.matchAll({ type: 'window' }))
      .then((clients) => clients.forEach((c) => {
        if (c.navigate) c.navigate(c.url).catch(() => {});
      }))
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  // 只处理 GET，POST（如保存配置）不受影响
  if (req.method !== 'GET') return;
  // 本地接口不缓存，直接走网络
  if (req.url.indexOf('/api/') >= 0) return;

  // 关键：cache: 'no-store' 强制跳过浏览器 HTTP 缓存。
  // 否则 GitHub Pages 给 HTML/JSON 的 max-age=600 会让 Service Worker
  // 每次"联网"都拿到 10 分钟前的旧页面，用户刷新也看不到更新。
  const fresh = new Request(req, { cache: 'no-store' });

  event.respondWith(
    fetch(fresh)
      .then((res) => {
        // 网络成功则更新缓存，保证下次断网看到的是最新数据
        const copy = res.clone();
        caches.open(CACHE).then((cache) => cache.put(req, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(req).then((hit) => hit || caches.match('./index.html')))
  );
});
