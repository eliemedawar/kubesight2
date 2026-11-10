#!/bin/bash
# Proof step 4 (provisioner pod): use the created cluster.
#   * a small Deployment of an image from Nexus, pulled with a Secret made from
#     the read-only Nexus account, in this cluster only;
#   * a PersistentVolumeClaim on oci-bv, mounted by that Deployment;
#   * an internal LoadBalancer Service with exactly the K21 annotations.
# Then, from a pod INSIDE the created cluster (the AI cluster cannot reach the
# load balancer subnet): the Service's internal address answers, and the
# volume is mounted and writable.
#
# Usage: step4-use.sh <image in registry.areeba.com with sh and wget, e.g. nginx:alpine>
#   then step4-use.sh cleanup   (Service first, so its load balancer goes; then the claim)
set -euo pipefail
. /opt/kubesight/proof/values.sh

kubeconfig_for "$(cluster_id)"
NS=kubesight-proof

if [ "${1:-}" = cleanup ]; then
  note "step 4 cleanup: Service, then the claim"
  kubectl -n "$NS" delete service web --wait=true --timeout=10m 2>&1 | tee -a "$EVIDENCE/step4-cleanup.txt"
  kubectl -n "$NS" delete deployment web --wait=true --timeout=5m 2>&1 | tee -a "$EVIDENCE/step4-cleanup.txt"
  kubectl -n "$NS" delete pod probe --ignore-not-found --wait=true 2>&1 | tee -a "$EVIDENCE/step4-cleanup.txt"
  kubectl -n "$NS" delete pvc data --wait=true --timeout=10m 2>&1 | tee -a "$EVIDENCE/step4-cleanup.txt"
  kubectl delete namespace "$NS" --wait=true --timeout=10m 2>&1 | tee -a "$EVIDENCE/step4-cleanup.txt"
  note "step 4 cleanup done; now step5-destroy.sh"
  exit 0
fi

IMAGE=${1:?usage: step4-use.sh <registry.areeba.com/...image> | cleanup}
case "$IMAGE" in registry.areeba.com/*) ;; *) echo "The image must come from registry.areeba.com." >&2; exit 1;; esac

note "step 4: namespace and pull Secret (read-only Nexus account)"
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -
read -r -p "Read-only Nexus user: " NEXUS_USER
read -r -s -p "Its password (not shown, not logged): " NEXUS_PASSWORD; echo
# The password goes through stdin only: never a command-line argument.
NEXUS_USER="$NEXUS_USER" NEXUS_PASSWORD="$NEXUS_PASSWORD" python3 - "$NS" <<'EOF' | kubectl apply -f - >/dev/null
import base64, json, os, sys
auth = base64.b64encode(f"{os.environ['NEXUS_USER']}:{os.environ['NEXUS_PASSWORD']}".encode()).decode()
config = {"auths": {"registry.areeba.com": {"auth": auth}}}
print(json.dumps({"apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/dockerconfigjson",
                  "metadata": {"name": "nexus-pull", "namespace": sys.argv[1]},
                  "data": {".dockerconfigjson": base64.b64encode(json.dumps(config).encode()).decode()}}))
EOF
unset NEXUS_PASSWORD
echo "Secret nexus-pull created in $NS (in the created cluster only)."

note "step 4: Deployment, claim, internal LoadBalancer Service"
sed "s#__IMAGE__#$IMAGE#g" /opt/kubesight/proof/step4.yaml > "$PROOF/step4.yaml"
kubectl apply -f "$PROOF/step4.yaml" 2>&1 | tee "$EVIDENCE/step4-apply.txt"
kubectl -n "$NS" rollout status deployment/web --timeout=10m 2>&1 | tee -a "$EVIDENCE/step4-apply.txt"
kubectl -n "$NS" get pvc data -o wide 2>&1 | tee "$EVIDENCE/step4-pvc.txt"

echo "Waiting for the load balancer's internal address..."
ip=""
for _ in $(seq 1 60); do
  ip=$(kubectl -n "$NS" get service web -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
  [ -n "$ip" ] && break
  sleep 10
done
kubectl -n "$NS" get service web -o yaml > "$EVIDENCE/step4-service.yaml"
kubectl -n "$NS" describe service web > "$EVIDENCE/step4-service-describe.txt" 2>&1 || true
if [ -z "$ip" ]; then
  note "step 4: NO load balancer address after 10 minutes (see step4-service-describe.txt)"
  exit 1
fi
case "$ip" in "$LB_SUBNET_PREFIX"*) note "load balancer address $ip (in service_lb_subnet 10.43.20.0/25)";;
  *) note "load balancer address $ip is NOT in 10.43.20.0/25";; esac

note "step 4: from inside the created cluster"
kubectl -n "$NS" run probe --image="$IMAGE" --restart=Never \
  --overrides='{"spec":{"imagePullSecrets":[{"name":"nexus-pull"}]}}' \
  --command -- sh -c "for i in 1 2 3 4 5 6; do wget -q -T 5 -O- http://$ip/ >/dev/null && echo LB answered && exit 0; sleep 10; done; echo LB did not answer; exit 1"
kubectl -n "$NS" wait pod/probe --for=jsonpath='{.status.phase}'=Succeeded --timeout=5m || true
kubectl -n "$NS" logs probe 2>&1 | tee "$EVIDENCE/step4-lb-probe.txt"
kubectl -n "$NS" exec deploy/web -- sh -c 'echo "proof $(date -u)" > /data/proof.txt && cat /data/proof.txt && df -h /data' \
  2>&1 | tee "$EVIDENCE/step4-volume.txt"
kubectl -n "$NS" get pods -o jsonpath='{range .items[*]}{.metadata.name}{"  "}{.spec.containers[*].image}{"\n"}{end}' \
  | tee "$EVIDENCE/step4-images.txt"
note "step 4 done; when the evidence is saved: step4-use.sh cleanup"
