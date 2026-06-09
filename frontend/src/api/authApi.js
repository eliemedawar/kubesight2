import { setStoredSession } from "../authStorage";
import { request } from "./client";

export const login = async (username, password) => {
  const data = await request("/api/auth/login", {
    method: "POST",
    body: { username, password },
    auth: false,
  });
  setStoredSession(data.token, data.user);
  return data;
};

export const logout = () => request("/api/auth/logout", { method: "POST" }).catch(() => null);

export const fetchCurrentUser = () => request("/api/auth/me");
