# The platform's values for the OKE proof (requirements version 2, section 3).
# Sourced by the step scripts; nothing secret here.
REGION=me-dubai-1
TENANCY=ocid1.tenancy.oc1..aaaaaaaaz2g7phoft6kww53xootx4iwuhvwa4oaropoghofhaai4dgdjviua
COMPARTMENT=ocid1.compartment.oc1..aaaaaaaajihvv42vja5ofypuvvhhxujlknsqwaolivymekmitqzhxxqesqgq
VCN=ocid1.vcn.oc1.me-dubai-1.amaaaaaabqxyblyabcseiwzpcvapendzs3ai4a2xky642sbmpuv6xotvdhla
ENDPOINT_SUBNET=ocid1.subnet.oc1.me-dubai-1.aaaaaaaa5qni34xzwbmw6zxji54smkwrg5qqa6kogpwiz2ablkmevfjj3m4a
WORKER_SUBNET=ocid1.subnet.oc1.me-dubai-1.aaaaaaaab7ia3gj4zpaavo4dgjnsnwqmbvj5ecmgkrwjnre4cwmcucr47hsq
LB_SUBNET=ocid1.subnet.oc1.me-dubai-1.aaaaaaaanjvtfokxg7fknvj33mbvio32p5pbbue5bito7xyxwiriueeuikvq
LB_SUBNET_PREFIX=10.43.20.
PROOF=/work/proof
EVIDENCE=/work/evidence
KUBECONFIG=/work/kubeconfig
export KUBECONFIG
mkdir -p "$PROOF" "$EVIDENCE"

# Workload identity must be on, and debug output off (K23).
: "${OCI_RESOURCE_PRINCIPAL_VERSION:?OCI_RESOURCE_PRINCIPAL_VERSION is not set in this pod}"
: "${OCI_RESOURCE_PRINCIPAL_REGION:?OCI_RESOURCE_PRINCIPAL_REGION is not set in this pod}"
[ "${OCI_CLI_AUTH:-}" = oke_workload_identity ] || { echo "OCI_CLI_AUTH must be oke_workload_identity" >&2; exit 1; }
unset TF_LOG TF_LOG_PATH OCI_CLI_DEBUG

note() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$EVIDENCE/notes.txt"; }

# The cluster this proof made (written by step 2).
cluster_id() {
  if [ -n "${CLUSTER_ID:-}" ]; then echo "$CLUSTER_ID"; return; fi
  if [ -s "$PROOF/cluster-id" ]; then cat "$PROOF/cluster-id"; return; fi
  echo "No cluster id: pass it as CLUSTER_ID=ocid1.cluster... (step 2 prints it)." >&2
  exit 1
}

kubeconfig_for() {
  oci ce cluster create-kubeconfig --cluster-id "$1" --region "$REGION" \
    --file "$KUBECONFIG" --token-version 2.0.0 --kube-endpoint PRIVATE_ENDPOINT --overwrite >/dev/null
}
