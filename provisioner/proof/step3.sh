#!/bin/bash
# Proof step 3: reach the created cluster's API with short-lived tokens only
# (K20). Each pod makes its own kubeconfig; the file holds no token, the exec
# plugin (oci ce cluster generate-token, OKE workload identity) makes one each
# time. Run the parts in this order, and keep every output:
#
#   3.1 backend pod:      step3.sh whoami         CLUSTER_ID=ocid1.cluster...
#   3.2 provisioner pod:  step3.sh whoami
#                         step3.sh can-i
#   3.3 provisioner pod:  step3.sh bind <the subject 3.1 printed>
#   3.4 provisioner pod:  step3.sh role
#   3.5 backend pod:      step3.sh can-i          CLUSTER_ID=ocid1.cluster...
set -euo pipefail
. /opt/kubesight/proof/values.sh

what=${1:?usage: step3.sh whoami | can-i | bind <subject> | role}
id=$(cluster_id)
kubeconfig_for "$id"
grep -q 'token:' "$KUBECONFIG" && { echo "The kubeconfig holds a token; refusing." >&2; exit 1; }
who=$(kubectl auth whoami -o jsonpath='{.status.userInfo.username}' 2>/dev/null || echo unknown)
tag=$(echo "$who" | tr -c 'A-Za-z0-9._-' '_' | cut -c1-60)

case "$what" in
  whoami)
    note "step 3 whoami as $who"
    kubectl auth whoami -o yaml 2>&1 | tee "$EVIDENCE/step3-whoami-$tag.yaml"
    ;;
  can-i)
    note "step 3 can-i --list as $who"
    kubectl auth can-i --list 2>&1 | tee "$EVIDENCE/step3-can-i-$tag.txt"
    ;;
  bind)
    subject=${2:?usage: step3.sh bind <subject from 3.1>}
    note "step 3.3: bind $subject to the built-in ClusterRole view"
    kubectl create clusterrolebinding kubesight-backend-view --clusterrole=view --user="$subject" \
      --dry-run=client -o yaml > "$PROOF/kubesight-backend-view.yaml"
    kubectl apply -f "$PROOF/kubesight-backend-view.yaml" 2>&1 | tee "$EVIDENCE/step3-bind.txt"
    ;;
  role)
    note "step 3.4: create the custom ClusterRole kubesight-proof-role (a copy of view)"
    kubectl get clusterrole view -o json | python3 -c '
import json, sys
role = json.load(sys.stdin)
print(json.dumps({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
                  "metadata": {"name": "kubesight-proof-role"}, "rules": role.get("rules") or []}))
' > "$PROOF/kubesight-proof-role.json"
    set +e
    kubectl create -f "$PROOF/kubesight-proof-role.json" 2>&1 | tee "$EVIDENCE/step3-role.txt"
    code=${PIPESTATUS[0]}
    set -e
    [ "$code" = 0 ] && note "custom ClusterRole: created" || note "custom ClusterRole: REFUSED (see step3-role.txt)"
    ;;
  *)
    echo "usage: step3.sh whoami | can-i | bind <subject> | role" >&2
    exit 2
    ;;
esac
