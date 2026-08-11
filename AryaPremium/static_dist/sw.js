const CACHE_NAME = "arya_image_service_worker_v1";

const IMAGE_HOSTS = [
  "storage.googleapis.com",
  "pocketfm.com",
  "reicon.dev",
  "iconsax.dev"
];

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key !== CACHE_NAME)
          .map((key) => caches.delete(key))
      )
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);

  const isImageReq =
    req.destination === "image" ||
    url.pathname.startsWith("/api/image") ||
    /\.(jpg|jpeg|png|webp|svg|gif)$/i.test(url.pathname) ||
    IMAGE_HOSTS.some((host) => url.hostname.includes(host));

  if (!isImageReq || req.method !== "GET") {
    return;
  }

  // Cache-First strategy for images
  event.respondWith(
    caches.match(req).then((cachedResponse) => {
      if (cachedResponse) {
        // Fetch background update when online
        if (navigator.onLine) {
          fetch(req)
            .then((networkResponse) => {
              if (networkResponse && networkResponse.status === 200) {
                caches.open(CACHE_NAME).then((cache) => {
                  cache.put(req, networkResponse);
                });
              }
            })
            .catch(() => {});
        }
        return cachedResponse;
      }

      // If not in cache, fetch network & cache response
      return fetch(req)
        .then((networkResponse) => {
          if (networkResponse && networkResponse.status === 200) {
            const responseClone = networkResponse.clone();
            caches.open(CACHE_NAME).then((cache) => {
              cache.put(req, responseClone);
            });
          }
          return networkResponse;
        })
        .catch(() => {
          // Return empty 204 response if completely offline and not in cache
          return new Response(null, { status: 204, statusText: "Offline" });
        });
    })
  );
});
