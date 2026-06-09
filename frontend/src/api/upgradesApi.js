import { request } from "./client";



export const getUpgradeInfo = (clusterId, targetVersion) =>

  request(

    `/api/upgrades/info?clusterId=${encodeURIComponent(clusterId)}&targetVersion=${encodeURIComponent(targetVersion)}`

  );



export const runUpgradePrecheck = (payload) =>

  request("/api/upgrades/precheck", { method: "POST", body: payload });



export const startUpgrade = (payload) =>

  request("/api/upgrades/start", { method: "POST", body: payload });

