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

exec python app.py
