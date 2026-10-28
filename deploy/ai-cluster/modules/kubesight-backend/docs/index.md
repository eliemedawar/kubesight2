# KubeSight backend

The backend is KubeSight's API and engine. It serves everything the web page
shows, keeps its state in PostgreSQL, and runs KubeSight's background work:
- the scheduler;
- the CI engine, which starts build Jobs in `kubesight-ci`;
- deploys and approvals;
- ticket automation.

It also hosts the MCP server that Hermes uses to act on KubeSight.

| | |
|---|---|
| Owner | DevOps team |
| Purpose | KubeSight's API, background work and MCP server |
| Version | image `registry.areeba.com/kubesight-backend:1.0.0`, chart `kubesight` 0.1.0 |
| Status | Planned: installed by CHG-KS-002 |
| Namespace | `kubesight` |
| Runs as | Deployment `kubesight-backend`, 1 replica, ServiceAccount `kubesight-backend` |
| Depends on | PostgreSQL through `pooler-kubesight` (platform), OpenBao (platform), the Kubernetes API, Hermes |
| Used by | the frontend (`/api`), Hermes (MCP), CI build pods and analysis Jobs (callbacks) |
| Secrets | `kubesight-secrets` ← `kv/kubesight/backend`; the platform's Secret `postgres` (database login); `regcred` ← `kv/kubesight/registry` |
| Change records | [CHG-KS-001](../../../changes/CHG-KS-001-openbao-secrets.md), [CHG-KS-002](../../../changes/CHG-KS-002-install.md) |
| Signed | ____ (fill in your name) |

- [Tutorial: your first session with the backend](tutorial.md)
- [How-to guides](how-to.md)
- [Explanation: why it is built this way](explanation.md)
- [Reference](reference.md)
