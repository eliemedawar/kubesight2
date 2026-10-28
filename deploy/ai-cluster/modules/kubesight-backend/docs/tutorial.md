# Tutorial: your first session with the backend

About fifteen minutes, read only. On the bastion, as `kprodai`. You will find
the backend, see its settings and secrets (names only), follow a request
through it, and read its logs.

## 1. Find it

```bash
kubectl -n kubesight get deploy,pods,svc,pvc -l areeba.com/module=kubesight-backend -o wide
```

You see one pod `kubesight-backend-…`, the Service `backend-service` on port
5000, and the claim `kubesight-backend-data` (50Gi, `oci-bv-retain`).

## 2. Ask it if it is healthy

```bash
kubectl -n kubesight exec deploy/kubesight-backend -- curl -s http://127.0.0.1:5000/health; echo
```

The same check runs as the pod's readiness and liveness probe.

## 3. See its settings

Plain settings are in the ConfigMap `kubesight-config`:

```bash
kubectl -n kubesight get configmap kubesight-config -o yaml | grep -E 'CI_KUBERNETES_NAMESPACE|CI_BUILDKIT|HERMES_API_URL|PUBLIC_BASE_URL|DB_POOL'
```

Secrets arrive from OpenBao. Look at their **names**, never their values:

```bash
kubectl -n kubesight get externalsecret kubesight-secrets; kubectl -n kubesight get secret postgres
kubectl -n kubesight get secret kubesight-secrets -o go-template='{{range $k,$v := .data}}{{$k}}{{"\n"}}{{end}}'
```

## 4. Follow a request

The web page calls `/api/...` on `kubesight.areeba.com`. The frontend's nginx
passes it to `backend-service:5000`. Watch it happen:

```bash
kubectl -n kubesight logs deploy/kubesight-backend -f --tail=20
```

Open KubeSight in your browser and click around: each page load shows up
here. Stop with Ctrl+C.

## 5. See the database connection

```bash
kubectl -n kubesight logs deploy/kubesight-backend --tail=300 | grep -iE 'migrat|database|leader'
```

The backend reaches PostgreSQL only through `pooler-kubesight.databases.svc`,
with `verify-full`. It created its own tables on its first start.

## 6. See what it starts in `kubesight-ci`

Run a build or an analysis in KubeSight, then:

```bash
kubectl -n kubesight-ci get jobs,pods,networkpolicies
```

The backend made these through the Kubernetes API, using its namespaced Roles
`kubesight-ci-launcher` and `kubesight-analysis-launcher`.

You have seen the backend end to end. The [how-to guides](how-to.md) cover the
day-to-day tasks.
