#!/bin/bash
# Proof step 2 (provisioner pod): create the tiny enhanced cluster with
# OpenTofu (main.tf next to this script). Usage:
#   step2-create.sh <owner work email> <expiry YYYY-MM-DD>
# Email the platform owner BEFORE running this (section 5, "Approvals").
set -euo pipefail
. /opt/kubesight/proof/values.sh

OWNER=${1:?usage: step2-create.sh <owner work email> <expiry YYYY-MM-DD>}
EXPIRY=${2:?usage: step2-create.sh <owner work email> <expiry YYYY-MM-DD>}
[[ $EXPIRY =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { echo "expiry must be YYYY-MM-DD" >&2; exit 1; }

cp /opt/kubesight/proof/main.tf "$PROOF/main.tf"
cp /opt/kubesight/tofu/.terraform.lock.hcl "$PROOF/.terraform.lock.hcl"
cd "$PROOF"

note "step 2: init"
tofu init -input=false -no-color -lockfile=readonly 2>&1 | tee "$EVIDENCE/step2-init.txt"

note "step 2: plan"
tofu plan -input=false -no-color -out=proof.tfplan \
  -var "owner=$OWNER" -var "expiry=$EXPIRY" 2>&1 | tee "$EVIDENCE/step2-plan.txt"

echo
read -r -p "Read the plan above. Type yes to create it: " answer
[ "$answer" = yes ] || { echo "Not created."; exit 1; }

note "step 2: apply"
set +e
tofu apply -input=false -no-color proof.tfplan 2>&1 | tee "$EVIDENCE/step2-apply.txt"
code=${PIPESTATUS[0]}
set -e

# The cluster's OCID as soon as it exists, even after a failed apply.
id=$(tofu state show -no-color oci_containerengine_cluster.cluster 2>/dev/null \
     | sed -n 's/^ *id *= *"\(ocid1\.cluster[^"]*\)".*/\1/p' | head -1 || true)
if [ -n "$id" ]; then
  echo "$id" > "$PROOF/cluster-id"
  note "cluster OCID: $id (kept in $PROOF/cluster-id)"
fi

if [ "$code" != 0 ]; then
  echo
  echo "The apply failed. Every refusal, word for word (send these to the platform owner):"
  grep -n -A6 -E 'NotAuthorizedOrNotFound|Error:' "$EVIDENCE/step2-apply.txt" | tee "$EVIDENCE/step2-refusals.txt" || true
  exit "$code"
fi

tofu output -no-color | tee "$EVIDENCE/step2-outputs.txt"
note "step 2 done"
