/* ai-batch-runner PWA service worker: static shell only, token-free.
 *
 * Ownership: exactly one cache ("pi-batch-pwa-v1"). Install precaches only
 * the Web App Manifest and the two icons; activate removes only older caches
 * carrying the "pi-batch-pwa-" prefix. An update therefore waits for a page
 * reload instead of force-activating a new worker over open clients. Nothing
 * is loaded from another origin and no code is injected or generated.
 *
 * Fetch policy: cache-first only for the closed static allowlist below.
 * Every other request (API routes, documents, navigations, non-GET,
 * cross-origin, query/fragment, or any request carrying an Authorization
 * header) returns before respondWith so the network behaviour is unchanged.
 * The worker never reads tokens, cookies, browser storage or the
 * Authorization header value - presence alone is enough to bypass.
 */
"use strict";

var PWA_CACHE = "pi-batch-pwa-v1";
var PWA_CACHE_PREFIX = "pi-batch-pwa-";
var PWA_STATIC_PATHS = [
  "/manifest.webmanifest",
  "/pwa/icon-192.png",
  "/pwa/icon-512.png"
];

self.addEventListener("install", function (event) {
  event.waitUntil(caches.open(PWA_CACHE).then(function (cache) {
    return cache.addAll(PWA_STATIC_PATHS.map(function (path) {
      return new Request(path, { cache: "no-store", credentials: "omit" });
    }));
  }));
});

self.addEventListener("activate", function (event) {
  event.waitUntil(caches.keys().then(function (names) {
    return Promise.all(names.filter(function (name) {
      return name.indexOf(PWA_CACHE_PREFIX) === 0 && name !== PWA_CACHE;
    }).map(function (name) {
      return caches.delete(name);
    }));
  }));
});

self.addEventListener("fetch", function (event) {
  if (!pwaOwnsRequest(event.request)) {
    return;
  }
  event.respondWith(caches.open(PWA_CACHE).then(function (cache) {
    return cache.match(event.request).then(function (cached) {
      if (cached) {
        return cached;
      }
      return fetch(event.request).then(function (response) {
        if (response && response.ok) {
          cache.put(event.request, response.clone());
        }
        return response;
      });
    });
  }));
});

function pwaOwnsRequest(request) {
  if (request.method !== "GET") {
    return false;
  }
  if (request.mode === "navigate") {
    return false;
  }
  if (request.headers.has("Authorization")) {
    return false;
  }
  if (request.url.indexOf("?") !== -1 || request.url.indexOf("#") !== -1) {
    return false;
  }
  var url;
  try {
    url = new URL(request.url);
  } catch (error) {
    return false;
  }
  if (url.origin !== self.location.origin) {
    return false;
  }
  if (url.search !== "" || url.hash !== "") {
    return false;
  }
  return PWA_STATIC_PATHS.indexOf(url.pathname) !== -1;
}
