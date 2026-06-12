import { request } from "./client";

export const listClients = () =>
  request("/api/clients");

export const getClient = (id) =>
  request(`/api/clients/${encodeURIComponent(id)}`);

export const createClient = (payload) =>
  request("/api/clients", { method: "POST", body: payload });

export const updateClient = (id, payload) =>
  request(`/api/clients/${encodeURIComponent(id)}`, { method: "PUT", body: payload });

export const deleteClient = (id) =>
  request(`/api/clients/${encodeURIComponent(id)}`, { method: "DELETE" });
