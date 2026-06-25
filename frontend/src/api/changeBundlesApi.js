import { request } from "./client";

const base = "/api/change-bundles";

export const getMyDraftBundle = () => request(`${base}/draft`, { method: "POST" });

export const listMyBundles = (query = {}) => request(`${base}/mine`, { query });

export const listChangeBundles = (query = {}) => request(base, { query });

export const listPendingBundles = (query = {}) => request(`${base}/pending`, { query });

export const getChangeBundle = (bundleId) =>
  request(`${base}/${encodeURIComponent(bundleId)}`);

export const addBundleItem = (bundleId, payload) =>
  request(`${base}/${encodeURIComponent(bundleId)}/items`, { method: "POST", body: payload });

export const updateBundleItem = (bundleId, itemId, payload) =>
  request(`${base}/${encodeURIComponent(bundleId)}/items/${encodeURIComponent(itemId)}`, {
    method: "PUT",
    body: payload,
  });

export const removeBundleItem = (bundleId, itemId) =>
  request(`${base}/${encodeURIComponent(bundleId)}/items/${encodeURIComponent(itemId)}`, {
    method: "DELETE",
  });

export const diffBundleItem = (bundleId, itemId) =>
  request(`${base}/${encodeURIComponent(bundleId)}/items/${encodeURIComponent(itemId)}/diff`);

export const submitChangeBundle = (bundleId, payload) =>
  request(`${base}/${encodeURIComponent(bundleId)}/submit`, { method: "POST", body: payload });

export const approveChangeBundle = (bundleId) =>
  request(`${base}/${encodeURIComponent(bundleId)}/approve`, { method: "POST" });

export const rejectChangeBundle = (bundleId, reason) =>
  request(`${base}/${encodeURIComponent(bundleId)}/reject`, {
    method: "POST",
    body: { reason: reason || "" },
  });

export const deleteChangeBundle = (bundleId) =>
  request(`${base}/${encodeURIComponent(bundleId)}`, { method: "DELETE" });
