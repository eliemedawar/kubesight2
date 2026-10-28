# KubeSight frontend

The frontend is what people open in their browser at `https://kubesight.areeba.com`.
It is nginx serving KubeSight's single-page app. On the AI cluster there is no
ingress controller, so the same nginx also forwards `/api` and `/health` to the
backend and serves HTTPS on port 8443, behind an internal OCI load balancer.

| | |
|---|---|
| Owner | DevOps team |
| Purpose | Serve the KubeSight web app; forward API calls to the backend; terminate TLS |
| Version | `registry.areeba.com/kubesight-frontend:1.0.0` (chart `kubesight` 0.1.0) |
| Status | Planned: installed by CHG-KS-002 |
| Namespace | `kubesight` |
| Runs as | Deployment `kubesight-frontend`, 2 replicas |
| Depends on | `kubesight-backend` (Service `backend-service:5000`); the TLS certificate in OpenBao |
| Used by | People, through the internal load balancer (Service `frontend-service`, port 443) |
| Secrets | `kubesight-tls` (from OpenBao `kv/kubesight/tls`: `tls.crt`, `tls.key`); `regcred` (from `kv/kubesight/registry`) to pull the image |
| Change records | [CHG-KS-001](../../../changes/CHG-KS-001-openbao-secrets.md) (secrets), [CHG-KS-002](../../../changes/CHG-KS-002-install.md) (install) |
| Signed | ____ (fill in your name) |

## Documents

- [Tutorial](tutorial.md): a first look at the running frontend, in about 15 minutes.
- [How-to guides](how-to.md): change the certificate, find the address, scale, check the proxy, roll back.
- [Explanation](explanation.md): why nginx does the ingress's job here.
- [Reference](reference.md): objects, ports, settings, network rules and labels.
