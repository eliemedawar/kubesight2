# Tutorial: look at the builders, and watch one work

About fifteen minutes, read only. You need the bastion
(`ssh kprodai@10.43.17.34`) and, for step 4, a KubeSight service whose
pipeline has a container image stage.

## 1. The two builders

```bash
kubectl -n kubesight-build get deploy,pods,svc -o wide -L areeba.com/module
```

You see:

- **the Deployment `buildkitd`,** with 2 pods, each `Running` and `1/1` ready.
  Look at the `NODE` column: the builders prefer different nodes, so losing one
  node leaves one builder;
- **the Service `buildkitd`:** one address for any builder;
- **the Service `buildkitd-pods`:** headless, one DNS record per builder. Build
  pods use it to try each builder in turn.

## 2. Ask a builder what it can do

```bash
kubectl -n kubesight-build exec deploy/buildkitd -- buildctl --addr tcp://127.0.0.1:1234 debug workers
```

It lists one `oci` worker and the platforms it builds for (`linux/amd64`).
This is the same command the readiness probe runs every 20 seconds.

## 3. How it runs

```bash
kubectl -n kubesight-build get deploy buildkitd -o jsonpath='{.spec.template.spec.containers[0].securityContext}'; echo
kubectl -n kubesight-build describe resourcequota
```

The first command shows uid 1000, `runAsNonRoot`, unconfined seccomp and
AppArmor, and **no** `capabilities`. That is exactly what the platform's fence
allows (the Explanation says why).

The second shows that two builders use the whole namespace's requests: 1 CPU
and 2Gi.

## 4. Watch a build use it

In KubeSight, run a build of a service that has a container image stage.
While it runs:

```bash
kubectl -n kubesight-build logs deploy/buildkitd -f --all-containers --prefix
```

Steps such as resolving the base image, running the Dockerfile's steps and
exporting to the registry show up on the builder that took the job. The build
pod in `kubesight-ci` shows the client side:

```bash
kubectl -n kubesight-ci logs <build pod> -c stage-<n>
```

## 5. See the traffic

```bash
kubectl -n kubesight-build get pods -o wide                      # which node
kubectl -n kube-system get pods -l k8s-app=cilium -o wide        # that node's agent
kubectl -n kube-system exec <cilium pod> -c cilium-agent -- hubble observe --namespace kubesight-build --last 50
```

You see the build pod from `kubesight-ci` arriving on 1234, then the builder
pulling the base image and pushing to the registry on 443 (or the registry's
port). Both are forwarded.
