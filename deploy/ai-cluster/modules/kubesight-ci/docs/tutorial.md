# Tutorial: watch a build run

About fifteen minutes. You will look at what the install prepared in
`kubesight-ci`, start one build from KubeSight, and follow its Job from start
to clean-up. Nothing here changes the cluster: the build is started from
KubeSight's UI, as any user would.

You need the bastion (`ssh kprodai@10.43.17.34`) and a KubeSight account that
can run a build of one service in the Service Catalog.

## 1. What the install prepared

```bash
kubectl -n kubesight-ci get serviceaccount,role,rolebinding -L areeba.com/module
```

You see two accounts:
- `kubesight-ci-build`, which build pods run as;
- `kubesight-analysis-worker`, which analysis pods run as.

Neither gets an API token. You also see two Roles, `kubesight-ci-launcher` and
`kubesight-analysis-launcher`, both bound to the backend's account in
`kubesight`. That is everything the backend may do here.

```bash
kubectl -n kubesight-ci describe resourcequota       # the platform's quota
kubectl -n kubesight-ci get limitrange               # the platform's defaults
kubectl -n kubesight-ci get networkpolicy            # default-deny, allow-dns
kubectl -n kubesight-ci get externalsecret regcred   # SecretSynced
```

## 2. Start a build

In KubeSight, open **Service Catalog**, pick a service and choose **Run build**.
Note the build number it shows.

In a second terminal, watch the namespace:

```bash
kubectl -n kubesight-ci get jobs,pods,networkpolicy,secrets -l app.kubernetes.io/name=kubesight-ci -w
```

Within a few seconds, three objects appear, all named `ci-b<build number>-<service>`:
- **a Job;**
- **a NetworkPolicy:** this build's own rule (DNS, the backend's port 5000, 443 out, BuildKit's port, any extra registry ports);
- **a Secret** named `...-secrets`: this build's tokens and stage secrets.

## 3. Follow the stages

Each stage is a container of the build's pod, run in order: `stage-0` (the
checkout), then `stage-1`, `stage-2` and so on. The last container,
`collector`, sends results back.

```bash
POD=$(kubectl -n kubesight-ci get pods -l kubesight.io/build-id=<build number> -o name)
kubectl -n kubesight-ci get $POD -o jsonpath='{range .status.initContainerStatuses[*]}{.name}{"\t"}{.state}{"\n"}{end}'
kubectl -n kubesight-ci logs $POD -c stage-0
kubectl -n kubesight-ci logs $POD -c stage-1 -f
```

The same lines appear in KubeSight's build drawer: the backend reads them from
here.

## 4. See the network rules work

Find the node the pod runs on, then ask that node's Cilium agent what the
build reached:

```bash
kubectl -n kubesight-ci get $POD -o wide
kubectl -n kube-system get pods -l k8s-app=cilium -o wide          # the agent on that node
kubectl -n kube-system exec <cilium pod> -c cilium-agent -- hubble observe --namespace kubesight-ci --last 50
```

You see DNS, the git host on 443, and the callback to `kubesight` on 5000,
all forwarded.

## 5. The clean-up

When the build ends, the Job finishes. It is deleted 900 seconds later
(`CI_JOB_TTL_SECONDS`), and its Secret and NetworkPolicy go with it.

```bash
kubectl -n kubesight-ci get jobs -l app.kubernetes.io/name=kubesight-ci
```

An analysis started from **Application Intelligence** goes the same way, under
the label `app.kubernetes.io/name=kubesight-application-analysis`, with Jobs
named `ks-app-analysis-<id>-...`.
