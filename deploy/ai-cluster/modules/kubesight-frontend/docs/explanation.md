# Explanation: why the frontend is built this way

## nginx does the ingress's job

In UAT, an ingress controller sends `/api` and `/health` to the backend and
everything else to the frontend. The AI cluster has no ingress controller. An
ingress controller is shared, cluster-wide infrastructure that belongs to the
platform, and the guide's way to reach users is an internal OCI load balancer.

So the frontend's own nginx does that routing. The chart replaces the image's
`default.conf` with its own (ConfigMap `kubesight-frontend-nginx`). It keeps the
image's rules for the app and adds two locations that forward to the backend. The
image is the same one UAT runs; only its configuration changes.

The `/api` location turns off buffering and waits up to 310 seconds. Log streams
are server-sent events, which buffering would hold back. The backend's own
timeout is 300 seconds. `client_max_body_size` is 512m, because mobile app
binaries (APK, AAB, IPA) are uploaded through `/api`.

## TLS ends in the pod

The load balancer passes TCP through on 443 to the pods' port 8443, as in the
guide's example. nginx holds the certificate, from the Secret `kubesight-tls`.
That Secret is filled from OpenBao by an ExternalSecret, so the certificate never
sits in Git, a values file or a Helm release.

nginx still listens on plain port 80 inside the cluster. The probes use it, and
so do in-cluster checks. The Service does not expose port 80.

## The load balancer keeps its own address as the source

The Service has `externalTrafficPolicy: Local`. With the default (`Cluster`), a
node passes traffic on to a pod on another node, and Cilium then sees that
node's address as the source. A network rule cannot name that address by subnet.
With `Local`, each node sends traffic only to its own frontend pods. The source
stays the load balancer's address in `10.43.10.0/24`, so the rule
`kubesight-frontend-ingress` can allow exactly that subnet. The load balancer's
health checks send traffic only to nodes that run a frontend pod.

That is also why there are two replicas: with `Local`, a node without a frontend
pod takes no traffic, and two pods keep the site up while one restarts.

## Closed by default

The namespace `kubesight` has a default-deny rule. The frontend may receive
traffic only:
- from the load balancers' subnet, on 8443;
- from other pods in `kubesight`.

It may send traffic only to DNS and to pods in `kubesight` (the backend).

## Still to improve

The image runs nginx as root, from `nginx:alpine`. The namespace enforces the
pod security level `baseline`, which allows that, and warns at `restricted`.
Moving to an unprivileged nginx image is a later change to the image itself.
