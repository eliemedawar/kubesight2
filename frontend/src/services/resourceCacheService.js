import { NAMESPACE_RESOURCE_LIST_KEYS } from "../lib/resourceTypes.js";

/** TTL for workload resource lists (pods, deployments, etc.). */
export const RESOURCE_CACHE_TTL_MS = 45_000;

/** TTL for slower-changing scope data (namespaces, settings). */
export const STATIC_CACHE_TTL_MS = 180_000;

function scopeKey(clusterId, namespace) {
  return `${clusterId || ""}:${namespace || ""}`;
}

function entryKey(clusterId, namespace, listKey) {
  return `${scopeKey(clusterId, namespace)}:${listKey}`;
}

/**
 * Module-level per-tab resource cache.
 * Designed for future watch/WebSocket subscribers via subscribe().
 */
class ResourceCacheService {
  constructor() {
    /** @type {Map<string, { items: unknown[], fetchedAt: number, error: string }>} */
    this.entries = new Map();
    /** @type {Set<() => void>} */
    this.subscribers = new Set();
    /** @type {Map<string, Promise<unknown[]>>} */
    this.inflight = new Map();
  }

  subscribe(listener) {
    this.subscribers.add(listener);
    return () => this.subscribers.delete(listener);
  }

  notify() {
    this.subscribers.forEach((listener) => {
      try {
        listener();
      } catch {
        // ignore subscriber errors
      }
    });
  }

  get(clusterId, namespace, listKey) {
    return this.entries.get(entryKey(clusterId, namespace, listKey)) || null;
  }

  set(clusterId, namespace, listKey, items, { error = "" } = {}) {
    this.entries.set(entryKey(clusterId, namespace, listKey), {
      items: Array.isArray(items) ? items : [],
      fetchedAt: Date.now(),
      error: error || "",
    });
    this.notify();
  }

  isStale(entry, ttlMs = RESOURCE_CACHE_TTL_MS) {
    if (!entry) {
      return true;
    }
    return Date.now() - entry.fetchedAt >= ttlMs;
  }

  hasFresh(clusterId, namespace, listKey, ttlMs = RESOURCE_CACHE_TTL_MS) {
    const entry = this.get(clusterId, namespace, listKey);
    return Boolean(entry && !this.isStale(entry, ttlMs));
  }

  clearScope(clusterId, namespace) {
    const prefix = `${scopeKey(clusterId, namespace)}:`;
    for (const key of [...this.entries.keys()]) {
      if (key.startsWith(prefix)) {
        this.entries.delete(key);
      }
    }
    for (const key of [...this.inflight.keys()]) {
      if (key.startsWith(prefix)) {
        this.inflight.delete(key);
      }
    }
    this.notify();
  }

  clearAll() {
    this.entries.clear();
    this.inflight.clear();
    this.notify();
  }

  getInflight(clusterId, namespace, listKey) {
    return this.inflight.get(entryKey(clusterId, namespace, listKey)) || null;
  }

  setInflight(clusterId, namespace, listKey, promise) {
    const key = entryKey(clusterId, namespace, listKey);
    this.inflight.set(key, promise);
    promise.finally(() => {
      if (this.inflight.get(key) === promise) {
        this.inflight.delete(key);
      }
    });
    return promise;
  }

  /** Merge cached lists for a namespace scope into a resources bucket. */
  buildResourcesBucket(clusterId, namespace) {
    return Object.fromEntries(
      NAMESPACE_RESOURCE_LIST_KEYS.map((listKey) => {
        const entry = this.get(clusterId, namespace, listKey);
        return [listKey, entry?.items || []];
      })
    );
  }
}

export const resourceCache = new ResourceCacheService();
