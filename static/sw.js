// AssetPilot — service worker minimal : rend l'application installable.
// Aucune mise en cache des pages : les données restent toujours à jour.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
