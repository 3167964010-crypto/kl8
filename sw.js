// 离线缓存：在线时网络优先（保证数据最新），断网时回退到最后一次缓存
// 这样手机断网仍能打开页面并查看上次同步的开奖与预测

const CACHE = 'kl8-cache-v1';

// 首次安装时预缓存核心页面与数据快照
const CORE = [
  './',
  './index.html',
  './algos.html',
  './prize.html',
  './snapshot.json',
  './algos_snapshot.json',
  './kl8_snapshot.json',
  './manifest.json',
  './gate.js'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(CORE))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  // 只处理 GET，POST（如保存配置）不受影响
  if (req.method !== 'GET') return;
  // 本地接口不缓存，直接走网络
  if (req.url.indexOf('/api/') >= 0) return;

  event.respondWith(
    fetch(req)
      .then((res) => {
        // 网络成功则更新缓存，保证下次断网看到的是最新数据
        const copy = res.clone();
        caches.open(CACHE).then((cache) => cache.put(req, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(req).then((hit) => hit || caches.match('./index.html')))
  );
});
