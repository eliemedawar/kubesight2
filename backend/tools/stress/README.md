# Stress test harness

Load-tests a local KubeSight against simulated clusters. Everything except the
clusters is real: the API server code, the database, the real `kubectl`
binary, caches, background jobs. Only the Kubernetes API servers are fake.
Python standard library only (plus Playwright for the page capture).

## Pieces

| File | What it does |
|---|---|
| `fake_k8s.py` | Read-only fake Kubernetes API servers (6 clusters, ~3,700 pods). Realistic latency, live churn, writes refused with 403. `/__control/{state,stats,mode,crash,marker}` to inspect or perturb a cluster. Run one process per cluster (`--only-index N`). |
| `setup_clusters.py` | Registers the fake clusters with KubeSight through its own API. |
| `seed_volume.py` | Fills a database with two years of volume: 60 users, 60k audit logs, 20k alerts, 12k CI builds, 3k deploy requests, 1.5k change bundles... |
| `serve.py` | Runs KubeSight shaped like production (1 process, 8 request threads, like `gunicorn -w 1 --threads 8`) and logs every request with time, queue wait, SQL and kubectl counts, plus every kubectl process and process vitals. `STRESS_PROFILE=1` adds a sampling profiler (`profile.txt`). |
| `capture_pages.py` | Opens every page in a real browser and records the API calls it makes on load and while it stays open (polling). Output feeds the load generator. |
| `loadgen.py` | Async virtual users replaying those page journeys (GET only), ramped in stages. |
| `analyze.py`, `compare_runs.py`, `kubectl_sources.py` | Per-endpoint, per-page, per-kubectl-call tables; one-line comparison of runs. |
| `freshness.py` | Crashes one pod in a fake cluster and times how long each page takes to show it. |
| `direct_parity.py`, `tls_parity.py` | Check `api/kube_direct.py` returns what kubectl returns (plain and TLS client-certificate clusters). |
| `probe.py`, `pages_summary.py`, `merge_pages.py` | Small helpers. |

## Running it

1. Start the fake clusters (one process each), writing kubeconfigs to a folder:
   `python fake_k8s.py --base-port 7101 --kubeconfig-dir <dir> --only-index 0` … `--only-index 5`
2. Start KubeSight on a scratch database (never the real one), with
   `KUBECONFIG` pointed at an empty kubeconfig so nothing reaches a real cluster,
   `KUBESIGHT_KUBECONFIG_DIR` at a scratch folder, and the SQLite file in WAL mode:
   `python tools/stress/serve.py --port 5091 --threads 8 --out <run dir>` (from `backend/`).
3. `python setup_clusters.py --kubeconfig-dir <dir>` then
   `python seed_volume.py --database-url sqlite:///<scratch db>` (with the server stopped).
4. `python capture_pages.py --out pages.json`, then
   `python loadgen.py --pages pages.json --out <run dir>/client --stages 10:180,25:180,50:240,100:240,150:180`.
5. `python analyze.py --server <run dir> --client <run dir>/client`.

On SQLite, enable WAL on the database file first: production runs Postgres,
and without WAL any background write makes concurrent requests fail with
"database is locked", which Postgres would never do.
