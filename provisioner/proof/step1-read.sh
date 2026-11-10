#!/bin/bash
# Proof step 1 (provisioner pod): list the compartments; read the VCN and the
# three subnets by OCID. Workload identity only: no key anywhere.
set -euo pipefail
. /opt/kubesight/proof/values.sh

refused=0
read_one() {
  local name=$1; shift
  echo "== $name"
  if "$@" > "$EVIDENCE/$name.json" 2> "$EVIDENCE/$name.err"; then
    echo "   answered ($(wc -c < "$EVIDENCE/$name.json") bytes, $EVIDENCE/$name.json)"
  else
    refused=1
    echo "   REFUSED, word for word:"
    sed 's/^/   | /' "$EVIDENCE/$name.err"
  fi
}

note "step 1 starts"
read_one step1-compartments oci iam compartment list --compartment-id "$TENANCY" \
  --compartment-id-in-subtree true --all --access-level ACCESSIBLE
read_one step1-vcn oci network vcn get --vcn-id "$VCN"
read_one step1-subnet-endpoint oci network subnet get --subnet-id "$ENDPOINT_SUBNET"
read_one step1-subnet-workers oci network subnet get --subnet-id "$WORKER_SUBNET"
read_one step1-subnet-lb oci network subnet get --subnet-id "$LB_SUBNET"

if [ -s "$EVIDENCE/step1-compartments.json" ]; then
  python3 - "$EVIDENCE/step1-compartments.json" <<'EOF'
import json, sys
data = json.load(open(sys.argv[1])).get("data") or []
print(f"   {len(data)} compartments; K8s-UAT listed: "
      f"{any(c.get('id','').endswith('qesqgq') for c in data)}")
EOF
fi
for f in step1-vcn step1-subnet-endpoint step1-subnet-workers step1-subnet-lb; do
  [ -s "$EVIDENCE/$f.json" ] && python3 - "$EVIDENCE/$f.json" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1])).get("data") or {}
print(f"   {d.get('display-name')}: {d.get('cidr-block')} {d.get('lifecycle-state')}"
      f"  public IPs refused: {d.get('prohibit-public-ip-on-vnic')}")
EOF
done
note "step 1 done (refused=$refused)"
exit $refused
