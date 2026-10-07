import { request } from "./client";

const base = "/api/promotions";

export const getPromotionBoard = ({ refresh = false } = {}) =>
  request(`${base}/board`, { query: refresh ? { refresh: 1 } : {} });

export const getPromotionActivity = (query = {}) => request(`${base}/activity`, { query });

export const getPromotionHistory = (repository) =>
  request(`${base}/history`, { query: { repository } });

export const checkPromotion = (payload) => request(`${base}/check`, { method: "POST", body: payload });

export const promote = (payload) => request(`${base}/promote`, { method: "POST", body: payload });

export const requestPromotionException = (payload) =>
  request(`${base}/exceptions`, { method: "POST", body: payload });

export const getPromotionSetup = () => request(`${base}/setup`);

export const createDefaultLadder = () => request(`${base}/setup/defaults`, { method: "POST" });

export const createEnvironment = (payload) =>
  request(`${base}/environments`, { method: "POST", body: payload });

export const updateEnvironment = (id, payload) =>
  request(`${base}/environments/${encodeURIComponent(id)}`, { method: "PUT", body: payload });

export const deleteEnvironment = (id) =>
  request(`${base}/environments/${encodeURIComponent(id)}`, { method: "DELETE" });

export const reorderEnvironments = (ids) =>
  request(`${base}/environments/order`, { method: "PUT", body: { ids } });

export const addEnvironmentBinding = (id, payload) =>
  request(`${base}/environments/${encodeURIComponent(id)}/bindings`, { method: "POST", body: payload });

export const removeEnvironmentBinding = (bindingId) =>
  request(`${base}/bindings/${encodeURIComponent(bindingId)}`, { method: "DELETE" });

export const updatePromotionPolicy = (payload) => request(`${base}/policy`, { method: "PUT", body: payload });

export const listPromotionNamespaces = (clusterId) =>
  request(`${base}/namespaces`, { query: { clusterId } });

export const getPromotionOverview = ({ refresh = false } = {}) =>
  request(`${base}/overview`, { query: refresh ? { refresh: 1 } : {} });

export const listPromotionReleases = (query = {}) => request(`${base}/releases`, { query });

export const createPromotionRelease = (payload) =>
  request(`${base}/releases`, { method: "POST", body: payload });

export const getNamespaceMap = (clusterId) => request(`${base}/namespace-map`, { query: { clusterId } });

export const previewBindingRule = (payload) =>
  request(`${base}/bindings/preview`, { method: "POST", body: payload });

export const getPromotionTimetable = () => request(`${base}/timetable`);

export const savePromotionSchedule = (environmentId, payload) =>
  request(`${base}/environments/${encodeURIComponent(environmentId)}/schedule`, { method: "PUT", body: payload });

export const setDepartureState = (payload) => request(`${base}/departures/state`, { method: "POST", body: payload });

export const setDepartureExcluded = (payload) =>
  request(`${base}/departures/exclude`, { method: "POST", body: payload });
