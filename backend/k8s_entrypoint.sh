#!/bin/sh
set -e

mkdir -p /root/.kube

# 1) Explicit path from env (ConfigMap)
if [ -n "${K8S_KUBECONFIG:-}" ] && [ -f "${K8S_KUBECONFIG}" ]; then
  export KUBECONFIG="${K8S_KUBECONFIG}"
# 2) Optional secret mount (host ~/.kube/config for multi-context)
elif [ -f /etc/kubeconfig/config ]; then
  export KUBECONFIG=/etc/kubeconfig/config
  export K8S_KUBECONFIG=/etc/kubeconfig/config
# 3) In-cluster ServiceAccount (same cluster the pod runs in)
elif [ -f /var/run/secrets/kubernetes.io/serviceaccount/token ]; then
  TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)
  CA=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
  SERVER="https://${KUBERNETES_SERVICE_HOST}:${KUBERNETES_SERVICE_PORT}"
  CFG=/root/.kube/config
  CONTEXT_NAME="${K8S_CONTEXT_NAME:-in-cluster}"

  kubectl config set-cluster in-cluster \
    --kubeconfig="${CFG}" \
    --server="${SERVER}" \
    --certificate-authority="${CA}" \
    --embed-certs=true
  kubectl config set-credentials kubesight-sa \
    --kubeconfig="${CFG}" \
    --token="${TOKEN}"
  kubectl config set-context "${CONTEXT_NAME}" \
    --kubeconfig="${CFG}" \
    --cluster=in-cluster \
    --user=kubesight-sa
  kubectl config use-context "${CONTEXT_NAME}" --kubeconfig="${CFG}"

  export KUBECONFIG="${CFG}"
  export K8S_KUBECONFIG="${CFG}"
fi

# This entrypoint only ever serves the API in a cluster, so it defaults to
# production. The backend keys its production checks off APP_ENV; set
# APP_ENV=development explicitly to run this image any other way.
export APP_ENV="${APP_ENV:-production}"
export FLASK_DEBUG="${FLASK_DEBUG:-false}"

# Background loops (scheduler tick, CI engine) elect a leader through a
# Postgres advisory lock and boot-time migrations are serialised, so extra
# workers or replicas no longer run each tick twice (see
# api/services/leader_election.py), and upgrade jobs are persisted. The
# default stays 1 because the TTL read caches are per process and only the
# worker that made a change clears them: with more workers a list can lag a
# write by up to its TTL (10-30s). Raise GUNICORN_WORKERS if that is acceptable.
# Never add --preload: each worker must start its own loops after fork.
# Threads provide concurrency for blocking kubectl/helm/log-stream calls.
exec gunicorn -w "${GUNICORN_WORKERS:-1}" --threads "${GUNICORN_THREADS:-8}" \
  -b 0.0.0.0:5000 \
  --timeout "${GUNICORN_TIMEOUT:-300}" "api:create_app()"
