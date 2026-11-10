#!/bin/bash
# Proof, last: delete the cluster (after step4-use.sh cleanup). Email the
# platform owner once it is gone. If OKE refused workload-identity tokens in
# step 3, do NOT run this yet: the platform asked to keep the cluster for up to
# two working days so they can look at it with you.
set -euo pipefail
. /opt/kubesight/proof/values.sh
cd "$PROOF"
[ -f terraform.tfstate ] || { echo "No local state in $PROOF. Delete by OCID instead:"; \
  echo "  oci ce cluster delete --cluster-id \$(cat $PROOF/cluster-id) --force"; exit 1; }

note "step 5: destroy plan"
tofu plan -destroy -input=false -no-color -out=destroy.tfplan \
  -var "owner=unused@areeba.com" -var "expiry=2000-01-01" 2>&1 | tee "$EVIDENCE/step5-plan.txt"
read -r -p "Type yes to delete everything above: " answer
[ "$answer" = yes ] || { echo "Nothing deleted."; exit 1; }
tofu apply -input=false -no-color destroy.tfplan 2>&1 | tee "$EVIDENCE/step5-destroy.txt"
note "step 5 done: the proof cluster is deleted"
