var CACHE = 'news-v5';   /* 改版后升一位，老用户下次打开就能拿到新版 */
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

// 缓存优先 + 后台更新：离线能看到上次的内容，有网时悄悄换新
self.addEventListener('fetch', function(e) {
  var req = e.request;
  if (req.method !== 'GET') return;
  var url = new URL(req.url);
  if (url.origin !== location.origin) return;   // 外域图片直接放行
  e.respondWith(
    caches.match(req).then(function(hit) {
      var net = fetch(req).then(function(resp) {
        if (resp && resp.status === 200) {
          var copy = resp.clone();
          caches.open(CACHE).then(function(c) { c.put(req, copy); });
        }
        return resp;
      }).catch(function() { return hit; });
      return hit || net;
    })
  );
});