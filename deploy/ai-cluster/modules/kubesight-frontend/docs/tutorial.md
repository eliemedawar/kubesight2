# Tutorial: your first look at the frontend

About fifteen minutes. Every step only reads. You need a session on the bastion
(`ssh kprodai@10.43.17.34`).

## 1. See it running

```bash
kubectl -n kubesight get deployment kubesight-frontend
kubectl -n kubesight get pods -l app=kubesight-frontend -o wide
```

You should see two pods, `Running` and `1/1` ready, on different nodes if the
scheduler could spread them.

## 2. See how people reach it

```bash
kubectl -n kubesight get service frontend-service
```

The `TYPE` is `LoadBalancer` and `EXTERNAL-IP` is an address in
`10.43.10.0/24`, the subnet `OCIDUAILB`. Port 443 goes to the pods' port 8443.
The DNS name `kubesight.areeba.com` points at that address.

## 3. Look at its nginx configuration

The chart replaces the image's own nginx configuration with its own:

```bash
kubectl -n kubesight get configmap kubesight-frontend-nginx -o jsonpath='{.data.default\.conf}'
```

Find the three parts:
- `listen 8443 ssl` with the certificate from `/etc/nginx/tls`;
- `location /api`, which goes to `backend-service.kubesight.svc.cluster.local:5000`;
- `location /`, which serves the app and falls back to `index.html`.

## 4. Check it works end to end

From inside a frontend pod, ask for the backend's health through nginx:

```bash
kubectl -n kubesight exec deploy/kubesight-frontend -- wget -qO- http://localhost/health; echo
```

An answer from the backend means the `/api` and `/health` forwarding works.

From the bastion, through the load balancer (if the firewall allows the bastion
to reach it):

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://kubesight.areeba.com/
```

`200` means the load balancer, the certificate and nginx all work.

## 5. Read its logs

```bash
kubectl -n kubesight logs deploy/kubesight-frontend --tail=30
```

Each line is one request: the address it came from, the path and the status.
Requests through the load balancer come from addresses in `10.43.10.0/24`.

## 6. See its network rules

```bash
kubectl -n kubesight get networkpolicy kubesight-frontend-ingress -o yaml
```

Only the load balancers' subnet may open port 8443. Pods in `kubesight` may
also reach it, through the namespace's `allow-dns-and-same-namespace` rule.

You have seen the frontend end to end. The [how-to guides](how-to.md) cover the
changes you may need to make.
