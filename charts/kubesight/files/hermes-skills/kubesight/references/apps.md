# Apps — what an application *is*

Two registries live here and they are easy to confuse. The names keep them
apart, and picking the wrong one wastes a call:

| | Answers | Tools |
|---|---|---|
| **Application Intelligence** | "what does this code do, and what is wrong with it" | `kubesight_applications_*`, `kubesight_application_*` |
| **Application services** | "is this application up, and which component is not" | `kubesight_app_services_*`, `kubesight_app_service_get` |

Neither is the CI catalog. A *buildable* service is `kubesight_services_list` in
[ci.md](ci.md).

## Application Intelligence — the analysis side

```
kubesight_applications_list
kubesight_application_get      {applicationId: 4}     ← includes analysis history
kubesight_application_analyses {applicationId: 4}     ← runs, newest first
kubesight_application_analysis {analysisId: 31}       ← one run in full
```

An analysis is Hermes reading a repository and producing findings, discovered
endpoints, dependencies, a runtime topology and a security posture.

**Quote the evidence, not the count.** Every finding carries the file and line
it came from. "Three high-severity findings" is a number; "`AuthFilter.java:88`
compares the token with `equals`, which is not constant-time" is the answer.
The per-finding evidence exists precisely so that a summary is never the last
word.

An analysis has a status. One that is `running` or `failed` has no findings to
read, and reporting its empty findings list as "nothing wrong" is a real
mistake — check the status before you read the results.

There is no tool that starts an analysis. It spawns a worker and reads a
repository, which is a decision rather than a question.

## Application services — the operational side

```
kubesight_app_services_list
kubesight_app_service_get {serviceId: 7}
```

One service is several deployments, and its health is the worst of them.
`kubesight_app_services_list` is the "is X up" call; `kubesight_app_service_get`
is the one that says **which component** is unhealthy, with each component's
live deployment and pods.

That distinction is the whole value. "Payments is degraded" sends somebody
looking through five deployments; "payments is degraded — the `payment-worker`
component has 0 of 2 replicas ready" sends them to one.

From there, `kubesight_pod_logs` in [observability.md](observability.md) on the
named component's pods.

## Who is connected

```
kubesight_clients_list
kubesight_components_list
```

`clients_list` is the answer to **"who would notice if this service went down"** —
registered external clients and, per client, which services they reach and over
what transport. Kubernetes does not record that relationship; KubeSight does,
and nothing in [clusters.md](clusters.md) can reconstruct it.

`components_list` is the infrastructure KubeSight tracks that is not a
Kubernetes workload: databases, brokers, gateways, external endpoints, each with
its last health check. When a service is unhealthy and every one of its pods is
Ready, this is the next place to look.
