import { request } from "./client";

export const getSettings = () => request("/api/settings");

export const updateSettings = (settingsPatch) =>
  request("/api/settings", { method: "PUT", body: settingsPatch });
