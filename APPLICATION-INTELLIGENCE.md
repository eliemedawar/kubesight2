# Application Intelligence

Application Intelligence analyzes a Bitbucket-hosted microservice without giving
Hermes or the KubeSight web process permission to modify source code or runtime
resources. Quick and Deep analysis are Phase 1. Phase 2 adds Build Verified,
deterministic API inventory, CycloneDX/SPDX SBOMs, finding workflow, analysis
comparison, and guarded Bitbucket pull requests. Phase 3 adds permission-scoped
Kubernetes workload evidence, source-to-runtime comparison, runtime topology,
review-only NetworkPolicy recommendations, and deployment-readiness gates.

## Architecture and trust boundaries

```text
Human user -> KubeSight API -> ApplicationAnalysis row
                              -> isolated Kubernetes Job
                                 init: read-only checkout
                                 main: deterministic discovery/scanners
                                       -> bounded redacted evidence
                                       -> Hermes as hermes-agent
                                       -> authenticated result callback
                                 finally: workspace deletion
```

- Flask never clones a repository or executes repository code.
- The checkout init container receives the read-only Bitbucket token through a
  short-lived Kubernetes Secret. The token is not placed in the Job command,
  application record, frontend response, logs, report, or Hermes package.
- The analyzer container does not receive the repository token.
- Callback tokens are random, stored only as SHA-256 hashes in PostgreSQL, and
  delivered to the Job through the short-lived Secret.
- Repositories are untrusted data. Generated/vendor/build directories, binaries,
  oversized files, oversized repositories, and excessive file counts are excluded.
- Configuration content is redacted before Hermes. Prompt-level repository
  instructions have no authority; the Hermes system prompt explicitly says so.
- Hermes output must match the exact JSON schema. Unknown fields and malformed
  results fail the analysis rather than being partially persisted.
- KubeSight calls Hermes through its authenticated OpenAI-compatible
  `/v1/chat/completions` endpoint. The response message must contain only the
  strict Application Intelligence JSON object.
- The worker is non-root, drops all capabilities, disallows privilege escalation,
  uses a read-only root filesystem, has no host mount or Docker socket, has bounded
  CPU/memory/ephemeral storage, an active deadline, limited retry, cancellation,
  TTL cleanup, and an ingress-deny/HTTPS-bounded NetworkPolicy.

Standard Kubernetes NetworkPolicy cannot restrict HTTPS by DNS name. The supplied
policy permits port 443 for Bitbucket and external Hermes endpoints. When
`HERMES_API_URL` is an in-cluster `.svc` URL, KubeSight adds a namespace-scoped
rule for only the configured Hermes port. Clusters with Cilium/Calico FQDN
policies should narrow external HTTPS to configured hosts. No package resolution
is performed in Phase 1.

## Permissions and service account

| Permission | Purpose |
|---|---|
| `applications:view` | View applications, history, results, findings, topology, and artifacts |
| `applications:manage` | Manage applications, credential profiles, finding workflow, and guarded pull requests |
| `applications:analyze` | Human permission to start or cancel analysis |
| `applications:execute` | Internal execution permission held only by `hermes-agent` |

`hermes-agent` is a non-interactive service account. Startup seeding sets
`is_service_account=true`, `interactive_login_enabled=false`, normalizes its role
to only `applications:execute`, and removes cluster access rules. It cannot log in,
deploy, mutate Kubernetes, manage users/roles/clusters/credentials/settings, or
write to Bitbucket. Human initiation and Hermes execution are both recorded.

## Bitbucket credential setup

From **Application Intelligence**, choose **Analyze Application**, then **Add
credential**. Supported read-only types are OAuth, repository access token,
project access token, workspace access token, and Atlassian API token. An
Atlassian API token must be paired with the email address of the Atlassian
account that created it. Normal account passwords are not accepted. Credential
responses expose only metadata and `secretConfigured`.

Existing profiles can be edited without replacing their encrypted secret. To
convert an API token that was previously saved as OAuth, select the profile,
choose **Edit**, change its type to **Atlassian API token**, enter the Atlassian
account email, and leave the token field blank.

Use the narrowest repository scope. Pull-request actions require a separate
write-enabled profile. KubeSight never uses the read-only analysis credential to
write, and a write-enabled profile cannot be selected for analysis.

After a valid repository URL and credential profile are selected, KubeSight
loads branches, tags, recent commits, and Dockerfile paths through the
read-only Bitbucket Cloud API. The stored token remains server-side. Both
dropdowns are searchable and allow an explicit custom revision or
repository-relative Dockerfile path when the API result is incomplete.

## Analysis modes

- **Quick**: metadata, configuration, dependency manifests, Dockerfile, and
  lightweight source analysis.
- **Deep**: full supported static analysis, topology, security review, API
  inventory, and Hermes reasoning.
- **Build Verified**: adds a credential-free build/test stage because builds and
  tests execute untrusted code. Locally this is a separate no-network Docker
  container. In Kubernetes it is a separate init container that receives the
  workspace but no Bitbucket, callback, or Hermes Secret mount. Its bounded
  report is validated as untrusted input before persistence.

## Phase 2 workflows

- Deterministic source patterns populate the API inventory even when Hermes
  omits a route. Hermes and deterministic entries are deduplicated.
- Every completed analysis emits CycloneDX 1.5 and SPDX 2.3 JSON SBOM artifacts.
- Finding status supports Open, Accepted, Resolved, False Positive, and Risk
  Accepted. False Positive and Risk Accepted require a reason. Every transition
  has a status-history row and human audit event.
- Analysis History compares two runs of the same application: per-severity
  movement, risk-level change, new/resolved findings, and added/removed/changed
  dependencies.
- A pull request requires explicit finding selection and a separate
  write-enabled credential profile. The worker checks out the exact analyzed
  commit, applies only selected patches, commits them on a non-default branch,
  runs credential-free Build Verified checks, pushes only after validation, and
  opens a Bitbucket pull request. It never pushes directly to the default branch.

## Phase 3 runtime intelligence

Map an application to a cluster, namespace, workload kind, and workload name in
the Analyze Application dialog. After source analysis completes, open
**Deployment Readiness** and choose **Refresh runtime evidence**. Collection is
independent from source analysis, so an unavailable cluster never fails or
invalidates the source result.

The caller must have `applications:analyze` plus KubeSight access to the exact
mapped cluster, namespace, and workload. Viewing a previously collected snapshot
also requires access to that mapping, preventing a user from reading runtime
evidence collected by a more privileged user.

Each collection stores a new `application_runtime_snapshots` row containing only
redacted evidence:

- workload replica state, pod readiness/restarts, image names, ports, probes,
  resources, service account, and safe security-context fields;
- environment variable names and Secret/ConfigMap references, never literal
  values;
- selecting Services, their ports, matching Ingress routes, and selecting
  NetworkPolicy summaries;
- explicit safe revision labels/annotations only.

Raw Secrets, ConfigMap data, literal environment values, Kubernetes tokens,
connection credentials, and arbitrary annotations are not persisted or passed to
Hermes. The collector performs only Kubernetes `get` and `list` operations.
Runtime topology edges are labeled **Runtime Observed**; source edges remain
**Source Inferred**.

Source-to-runtime results use the required labels `Matched`, `Missing`,
`Unexpected`, and `Cannot Verify`. The comparison covers source ports,
configuration names, required Secret references, Services, Ingress paths, health
probes, NetworkPolicy presence, deployed images, and revision metadata.
Readiness gates evaluate availability, immutable images, probes, resources,
restricted container security, network isolation, open Critical/High findings,
source drift, and Build Verified status.

The generated NetworkPolicy is a review-only downloadable YAML proposal. It is
never submitted to the Kubernetes API. Peer identity that cannot be proven from
runtime evidence is called out as a limitation.

## Scanner configuration

The worker discovers ecosystems deterministically, then invokes replaceable
adapters for Semgrep, Trivy, Syft, and Hadolint. An unavailable or failed scanner
becomes a safe warning; remaining evidence can complete as **Completed With
Warnings**. The worker image must place enabled scanners on `PATH`. Scanner name,
version, times, exit status, and redacted warning are stored.

Relevant environment:

| Variable | Default / purpose |
|---|---|
| `APPLICATION_ANALYSIS_WORKER_IMAGE` | `kubesight-backend:latest`; use a scanner-equipped immutable image |
| `APPLICATION_ANALYSIS_NAMESPACE` | `kubesight-analysis` |
| `APPLICATION_ANALYSIS_SERVICE_ACCOUNT` | `kubesight-analysis-worker` |
| `APPLICATION_ANALYSIS_CALLBACK_URL` | Backend worker-callback base URL |
| `APPLICATION_ANALYSIS_CPU_LIMIT` | `2` |
| `APPLICATION_ANALYSIS_MEMORY_LIMIT` | `4Gi` |
| `APPLICATION_ANALYSIS_EPHEMERAL_LIMIT` | `2Gi` |
| `APPLICATION_ANALYSIS_DEADLINE_SECONDS` | `1800` |
| `APPLICATION_BUILD_CPU_LIMIT` | `2` |
| `APPLICATION_BUILD_MEMORY_LIMIT` | `4Gi` |
| `APPLICATION_BUILD_COMMAND_TIMEOUT_SECONDS` | `600` |
| `APPLICATION_PULL_REQUEST_CALLBACK_URL` | Backend pull-request callback base URL |
| `APPLICATION_ANALYSIS_ARTIFACT_ROOT` | Persistent artifact directory |
| `APPLICATION_ANALYSIS_EGRESS_PROXY_URL` | Required for Kubernetes Build Verified/PR jobs; controlled proxy URL |
| `APPLICATION_ANALYSIS_EGRESS_PROXY_CIDR` | Required proxy IP/CIDR used by NetworkPolicy |
| `APPLICATION_ANALYSIS_EGRESS_PROXY_PORT` | Required proxy port |

### Local Docker execution

For local development, set
`APPLICATION_ANALYSIS_EXECUTION_MODE=local_docker` and build
`backend/Dockerfile.application-worker-local` as
`kubesight-application-worker:local`. The executor runs checkout, deterministic
discovery, scanners, and Hermes in a read-only, non-root Docker container.
Repository, callback, and Hermes credentials are mounted as temporary read-only
files and removed after the container exits. Build Verified uses three
containers over one disposable workspace: checkout with only the read token,
build/test with no network and no credential mount, then analysis with only the
callback and Hermes tokens.

The local worker joins `APPLICATION_ANALYSIS_LOCAL_DOCKER_NETWORK` so it can
reach the loopback-only Hermes Compose service, and calls the host backend via
`APPLICATION_ANALYSIS_LOCAL_CALLBACK_URL`. Production continues to use the
default `kubernetes` execution mode and the namespace/RBAC resources in
`k8s/application-analysis-worker.yaml`.
| `APPLICATION_ANALYSIS_JOB_TTL_SECONDS` | `900` |
| `APPLICATION_ANALYSIS_MAX_FILES` | `12000` |
| `APPLICATION_ANALYSIS_MAX_FILE_BYTES` | `1048576` |
| `APPLICATION_ANALYSIS_MAX_REPOSITORY_BYTES` | `524288000` |
| `HERMES_API_URL` | Required server-side Hermes endpoint |
| `HERMES_API_TOKEN` | Required server-side token; never sent to the frontend |
| `HERMES_APPLICATION_MODEL` | Hermes API model name; typically `hermes-agent` |
| `HERMES_ALLOW_LOCAL_HTTP` | Development-only opt-in for an HTTP endpoint whose hostname is loopback |
| `HERMES_SERVICE_NAMESPACE` | Optional explicit namespace for an in-cluster Hermes endpoint; normally derived from its `.svc` URL |
| `HERMES_SERVICE_PORT` | Optional explicit Hermes service port; normally derived from the URL |
| `APPLICATION_ANALYSIS_HERMES_TIMEOUT_SECONDS` | End-to-end Hermes request timeout; default `180` |
| `APPLICATION_ANALYSIS_HERMES_MAX_BYTES` | Maximum request size sent to Hermes; default `4000000` |
| `APPLICATION_ANALYSIS_HERMES_RESPONSE_MAX_BYTES` | Maximum Hermes response size; default `4000000` |

Apply [k8s/application-analysis-worker.yaml](k8s/application-analysis-worker.yaml)
after adapting the launcher RoleBinding ServiceAccount name/namespace.

### Hermes API server

Use a dedicated Hermes profile for source analysis. In its runtime environment:

```text
API_SERVER_ENABLED=true
API_SERVER_HOST=0.0.0.0
API_SERVER_PORT=8642
API_SERVER_KEY=<dedicated secret>
API_SERVER_MODEL_NAME=hermes-agent
```

The standard Hermes API-server toolset includes terminal, file, browser, and web
tools. Application Intelligence must not use that default. Explicitly disable
tools and globally enabled MCP servers for the API-server platform:

```yaml
platform_toolsets:
  api_server:
    - no_mcp
```

Start `hermes gateway`, expose port 8642 only through an internal Service, and
set KubeSight to an endpoint such as:

```text
HERMES_API_URL=http://hermes.hermes.svc.cluster.local:8642/v1/chat/completions
HERMES_API_TOKEN=<same dedicated secret>
HERMES_APPLICATION_MODEL=hermes-agent
```

Do not give this profile `KUBESIGHT_API_TOKEN`; the isolated worker owns the
per-analysis callback. Keep Hermes runtime homes, `.env`, `auth.json`, SSH
material, histories, sessions, and state databases outside the KubeSight source
tree and container build context.

### Production Hermes deployment

Production must use a dedicated provider API key and gateway token. Do not copy
the local Codex OAuth mount, local development token, or `backend/.env` into the
cluster. The supplied deployment is intentionally stateless: `/opt/data` is an
`emptyDir`, while `config.yaml` comes from a ConfigMap. Application Intelligence
sends one complete request at a time and does not require Hermes session
persistence.

1. Build and push the dedicated no-tools image. Replace the example registry and
   use an immutable version or digest:

   ```powershell
   $HermesImage = "registry.example.com/kubesight/hermes-application-intelligence:1.0.0"
   docker build -f Dockerfile.hermes-production -t $HermesImage `
     hermes/hermes/.hermes/hermes-agent
   docker push $HermesImage
   ```

   Behind a TLS-inspecting corporate proxy, pass its public root CAs as BuildKit
   secrets (the Dockerfile accepts up to two):

   ```powershell
   docker build -f Dockerfile.hermes-production -t $HermesImage `
     --secret id=corporate_ca_1,src=path/to/corporate-root.pem `
     --secret id=corporate_ca_2,src=path/to/second-root.pem `
     hermes/hermes/.hermes/hermes-agent
   ```

2. Replace `registry.example.com/...:replace-me` in
   `k8s/hermes-application-intelligence.yaml` with that immutable image. Review
   the model in its ConfigMap. The supplied profile uses two turns, low reasoning,
   bounded output, no environment probe, and `no_mcp` for faster structured
   responses.

3. Create the namespace and Secrets. Generate one random gateway token and store
   the same value under different key names in the Hermes and KubeSight
   namespaces. The provider key exists only in the Hermes namespace:

   ```powershell
   kubectl create namespace kubesight-hermes --dry-run=client -o yaml | kubectl apply -f -

   $TokenBytes = New-Object byte[] 32
   [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($TokenBytes)
   $HermesGatewayToken = -join ($TokenBytes | ForEach-Object { $_.ToString("x2") })
   $OpenAIKey = Read-Host "OpenAI API key"

   kubectl -n kubesight-hermes create secret generic hermes-application-intelligence `
     --from-literal=API_SERVER_KEY=$HermesGatewayToken `
     --from-literal=OPENAI_API_KEY=$OpenAIKey

   kubectl -n kubesight create secret generic kubesight-hermes-credentials `
     --from-literal=HERMES_API_TOKEN=$HermesGatewayToken
   ```

   In a managed environment, create these values through External Secrets,
   Sealed Secrets, SOPS, or the platform secret manager instead of shell history.
   Never commit a Secret manifest containing real values.

4. Apply Hermes, the analysis worker boundary, and the KubeSight non-secret
   settings:

   ```powershell
   kubectl apply -f k8s/hermes-application-intelligence.yaml
   kubectl apply -f k8s/application-analysis-worker.yaml
   kubectl apply -f k8s/application-intelligence-storage.yaml
   kubectl apply -f k8s/kubesight-hermes-integration.yaml
   kubectl apply -f k8s/application-intelligence-runtime-reader.yaml
   ```

   The runtime-reader manifest creates a read-only ClusterRole but deliberately
   creates no binding. For an in-cluster connection, bind it separately in each
   target namespace. This example grants the KubeSight backend access only in
   `payments`:

   ```powershell
   kubectl -n payments create rolebinding kubesight-application-runtime-reader `
     --clusterrole=kubesight-application-runtime-reader `
     --serviceaccount=kubesight:kubesight-backend
   ```

   Repeat only for namespaces that Application Intelligence should observe. For
   external cluster profiles, grant the configured identity equivalent `get` and
   `list` access to Deployments, StatefulSets, DaemonSets, ReplicaSets, Pods,
   Services, Ingresses, and NetworkPolicies. Secret and ConfigMap read permission
   is not required.

5. Add the ConfigMap and Secret to the KubeSight backend Deployment. If the
   Deployment is managed directly rather than by Helm, these commands add the
   environment references:

   ```powershell
   kubectl -n kubesight set env deployment/kubesight-backend `
     --from=configmap/kubesight-hermes-integration
   kubectl -n kubesight set env deployment/kubesight-backend `
     --from=secret/kubesight-hermes-credentials
   ```

   Build and push `backend/Dockerfile.application-worker`, then replace
   `APPLICATION_ANALYSIS_WORKER_IMAGE` in the ConfigMap with its immutable
   digest. Configure the three `APPLICATION_ANALYSIS_EGRESS_PROXY_*` variables
   on the backend for Build Verified and PR jobs. The launcher refuses those
   modes in Kubernetes without a controlled proxy; the build container receives
   no proxy environment or credentials. For Helm/GitOps, express the equivalent
   `envFrom` entries and artifact
   PVC mount in values or the Deployment template so reconciliation does not
   remove them:

   ```yaml
   envFrom:
     - configMapRef:
         name: kubesight-hermes-integration
     - secretRef:
         name: kubesight-hermes-credentials
   volumeMounts:
     - name: application-intelligence-artifacts
       mountPath: /var/lib/kubesight/application-analysis-artifacts
   volumes:
     - name: application-intelligence-artifacts
       persistentVolumeClaim:
         claimName: kubesight-application-intelligence-artifacts
   ```

6. Verify the rollouts and then run **Test Hermes** from the Analyze Application
   dialog:

   ```powershell
   kubectl -n kubesight-hermes rollout status deployment/hermes-application-intelligence
   kubectl -n kubesight rollout status deployment/kubesight-backend
   kubectl -n kubesight-hermes get pods,service,networkpolicy
   kubectl -n kubesight-hermes logs deployment/hermes-application-intelligence
   ```

The NetworkPolicy accepts port 8642 only from `kubesight` and
`kubesight-analysis`. It allows public HTTPS because standard Kubernetes
NetworkPolicy cannot select `api.openai.com` by name; use a Cilium or Calico FQDN
policy to narrow that rule where supported. If the cluster uses a private model
gateway, change the provider/base URL and restrict egress to that gateway.
ConfigMap changes require a Hermes rollout restart. Gateway-token rotation
requires updating both namespace Secrets and restarting Hermes and the backend;
provider-key rotation restarts only Hermes.

### Local Hermes test

The repository includes a loopback-only, minimal Docker profile for testing the
existing local Hermes runtime with KubeSight. It builds the gateway from the
local Hermes source without browser, dashboard, or terminal tooling:

```powershell
docker compose -f docker-compose.hermes-local.yml up -d
docker compose -f docker-compose.hermes-local.yml ps
```

The profile exposes the API only at `127.0.0.1:8642`. The ignored
`backend/.env` points KubeSight to that endpoint and explicitly enables HTTP only
for a loopback hostname. HTTP to LAN or public hosts remains rejected. The
checked-in compose token is for local development only; replace both matching
values before adapting this configuration for a shared environment.
The local profile mounts the current Codex CLI `auth.json` read-only so Hermes
can recover when an older copied OAuth refresh token has already been rotated.
Hermes stores its imported provider state in the ignored local runtime directory.

Open **Application Intelligence → Analyze Application → Test Hermes** to exercise
the authenticated chat-completions endpoint, configured model provider, and
strict result schema without sending repository content. Stop the local service
with:

```powershell
docker compose -f docker-compose.hermes-local.yml down
```

## Retention, artifacts, and reports

Cloned files live only on the Job `emptyDir` and are explicitly removed in a
`finally` block; Kubernetes also deletes the volume with the Pod. Analysis rows
retain redacted structured evidence, normalized findings/topology, scanner
metadata, and audit attribution. Artifact downloads validate authorization,
storage-root confinement, and SHA-256 checksum. Phase 2 stores JSON reports,
CycloneDX/SPDX SBOMs, build-verification reports, hardened Dockerfile proposals,
and reviewable patches.

Hermes proposals (including Dockerfile suggestions) are review-only. They are not
written to the repository or applied to Kubernetes.

## API

- `GET/POST /api/applications`
- `GET/PATCH/DELETE /api/applications/:id`
- `POST/GET /api/applications/:id/analyses`
- `GET /api/application-analyses/:id`
- `POST /api/application-analyses/:id/cancel`
- `GET /api/application-analyses/:id/findings`
- `PATCH /api/application-findings/:id`
- `GET /api/application-findings/:id/patch`
- `GET /api/application-analyses/:id/compare`
- `GET/POST /api/application-analyses/:id/pull-requests`
- `GET /api/application-analyses/:id/topology`
- `GET /api/application-analyses/:id/apis`
- `GET /api/application-analyses/:id/configuration`
- `GET /api/application-analyses/:id/runtime`
- `POST /api/application-analyses/:id/runtime/collect`
- `GET /api/application-analyses/:id/runtime/network-policy`
- `GET /api/application-analyses/:id/artifacts`
- `GET /api/application-artifacts/:id/download`
- `GET/POST /api/bitbucket-credential-profiles`
- `DELETE /api/bitbucket-credential-profiles/:id`
- `POST /api/application-intelligence/bitbucket/revisions`
- `POST /api/application-intelligence/bitbucket/dockerfiles`
- `POST /api/application-intelligence/hermes/test`

Worker callback routes use a per-analysis bearer token and are not human APIs.

## How risk is expressed

KubeSight publishes no model-generated score. Risk posture is computed from the
findings actually persisted for a run: per-severity counts, the counts remaining
open, and a risk level equal to the highest open severity. Both are reproducible
from the findings table, and their direction is unambiguous.

Two signals qualify every result and are shown next to it:

- **Evidence coverage** — which deterministic scanners produced evidence. When a
  scanner is missing from the worker image, its output is absent rather than
  clean, and the UI says so instead of showing an empty list.
- **Build verification** — the status of KubeSight's own credential-free build
  stage. Hermes is explicitly forbidden from asserting build, test, scanner, or
  runtime outcomes, and any such claim is stripped before persistence.

A run that failed or was cancelled reports "Not assessed" rather than a passing
posture: it produced no findings because it stopped, not because the repository
is clean.

Each finding stores the literal source observation it rests on. A finding with no
recorded evidence is labeled as a lead rather than a confirmed defect.

## Topology and ports

The Architecture tab renders source-inferred communications with the shared
topology viewer used elsewhere in KubeSight, plus a table of destination,
protocol, port, direction, and endpoint. Edge labels carry the wire protocol and
port.

Ports and protocols appear only where a literal value was found in the evidence —
a connection URL, an `EXPOSE`, or a configuration value. A protocol's conventional
default port is never filled in, because a guessed port is indistinguishable from
an observed one once drawn on a diagram. Dependencies with no stated port say
"not stated" rather than showing a plausible number.

Route inventory separates routes a service **serves** from routes it **calls**.
Spring Feign client interfaces declare consumed endpoints with the same
annotations a controller uses for served ones, so direction is decided by a
file-level marker (`@FeignClient`, `@HttpExchange`, `@RegisterRestClient`) rather
than by the model. Analyses recorded before this distinction show their routes as
"Direction not determined" until re-analyzed.

## AI limitations

Hermes identifies evidence-backed risks and observations; it does not prove
business logic correct. Confidence is displayed separately from severity.
Source-derived topology is labeled by evidence state and is not runtime truth.
Missing runtime access never fails source analysis. Finding wording and severity
can shift between runs on the same commit, so history comparison counts churn by
fingerprint and may report a re-worded finding as both new and resolved.

## Troubleshooting

| Symptom | Check |
|---|---|
| Scheduling failure | launcher RoleBinding, namespace, `kubectl`, worker image |
| Checkout failure | HTTPS Bitbucket URL, revision, read-only profile scope |
| Repository too large | size/file/file-count limits and monorepo subdirectory |
| Scanner warning | executable and rules/database available in worker image |
| Hermes timeout/malformed response | endpoint, model, timeout, strict JSON schema |
| Stuck job | active deadline, Pod events, callback URL/NetworkPolicy |
| Cleanup warning | Pod volume permissions and TTL controller |
| 403 | `applications:view/manage/analyze` on the human role |

Audit logs intentionally exclude source content and secret values.
