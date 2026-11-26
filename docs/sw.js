var CACHE = 'news-v6';   /* 改版后升一位，老用户下次打开就能拿到新版 */
var CORE = ['./', './index.html', './manifest.json', './icon-180.png',
            './data.json', './us.json'];

self.addEventListener('install', function(e) {
  e.waitUntil(caches.open(CACHE).then(function(c) {
    // 逐个缓存，某个资源暂时取不到也不让整个安装失败
    return Promise.all(CORE.map(function(u) {
      return c.add(u).catch(function() {});
    }));
  }).then(function() { return self.skipWaiting(); }));
});

self.addEventListener('activate', function(e) {
  e.waitUntil(caches.keys().then(function(keys) {
    return Promise.all(keys.filter(function(k) { return k !== CACHE; })
      .map(function(k) { return caches.delete(k); }));
  }).then(function() { return self.clients.claim(); }));
});

// 网络优先：有网就先拿线上最新的（避免"明明更新了手机却显示旧的"），
// 只有联网失败才回退缓存，保证离线仍能看。
self.addEventListener('fetch', function(e) {
  var req = e.request;
  if (req.method !== 'GET') return;
  var url = new URL(req.url);
  if (url.origin !== location.origin) return;   // 外域图片直接放行
  e.respondWith(
    fetch(req).then(function(resp) {
      if (resp && resp.status === 200) {
        var copy = resp.clone();
        caches.open(CACHE).then(function(c) { c.put(req, copy); });
      }
      return resp;
    }).catch(function() {
      return caches.match(req);   // 断网/超时才用缓存兜底
    })
  );
});