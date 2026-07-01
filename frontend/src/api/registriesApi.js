import { request } from "./client";

export const listRegistries = () => request("/api/registries");

export const createRegistry = (payload) =>
  request("/api/registries", { method: "POST", body: payload });

export const updateRegistry = (id, payload) =>
  request(`/api/registries/${id}`, { method: "PUT", body: payload });

export const deleteRegistry = (id) =>
  request(`/api/registries/${id}`, { method: "DELETE" });

export const testRegistry = (id) =>
  request(`/api/registries/${id}/test`, { method: "POST" });

export const checkImage = (image) =>
  request("/api/registries/check-image", { method: "POST", body: { image } });
