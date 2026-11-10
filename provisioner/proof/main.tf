# OKE proof, step 2: one tiny ENHANCED cluster in K8s-UAT, by OKE workload
# identity (no key anywhere), following K6 to K11 and K17 of the platform's
# requirements (version 2, 2026-11-10).
#
# State is local, in the pod's emptyDir (/work/proof), with no encryption: it
# holds OCIDs and settings only, and goes with the pod. Note the cluster OCID
# as soon as it exists (step2-create.sh prints it and keeps it in
# /work/proof/cluster-id) so it can be deleted with the CLI if the pod is lost.

terraform {
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "= 9.11.0"
    }
  }
}

provider "oci" {
  auth   = "OKEWorkloadIdentity"
  region = var.region
}

# ---------------------------------------------------------------------------
# The platform's values (section 3)
# ---------------------------------------------------------------------------

variable "region" {
  default = "me-dubai-1"
}
variable "availability_domain" {
  default = "wchx:ME-DUBAI-1-AD-1"
}
variable "compartment_id" { # DevOps:Kubernetes:K8s-UAT
  default = "ocid1.compartment.oc1..aaaaaaaajihvv42vja5ofypuvvhhxujlknsqwaolivymekmitqzhxxqesqgq"
}
variable "vcn_id" { # OCIDUVCNK8SUAT 10.43.20.0/24
  default = "ocid1.vcn.oc1.me-dubai-1.amaaaaaabqxyblyabcseiwzpcvapendzs3ai4a2xky642sbmpuv6xotvdhla"
}
variable "vcn_cidr" {
  default = "10.43.20.0/24"
}
variable "endpoint_subnet_id" { # OCIDUSUBK8SUATMASTER 10.43.20.128/26
  default = "ocid1.subnet.oc1.me-dubai-1.aaaaaaaa5qni34xzwbmw6zxji54smkwrg5qqa6kogpwiz2ablkmevfjj3m4a"
}
variable "endpoint_subnet_cidr" {
  default = "10.43.20.128/26"
}
variable "worker_subnet_id" { # OCIDUSUBK8SUATWORKERS 10.43.20.192/26
  default = "ocid1.subnet.oc1.me-dubai-1.aaaaaaaab7ia3gj4zpaavo4dgjnsnwqmbvj5ecmgkrwjnre4cwmcucr47hsq"
}
variable "worker_subnet_cidr" {
  default = "10.43.20.192/26"
}
variable "service_lb_subnet_id" { # OCIDUSUBK8SUATLB 10.43.20.0/25
  default = "ocid1.subnet.oc1.me-dubai-1.aaaaaaaanjvtfokxg7fknvj33mbvio32p5pbbue5bito7xyxwiriueeuikvq"
}
variable "service_lb_subnet_cidr" {
  default = "10.43.20.0/25"
}
variable "kubesight_source_cidr" { # OCIDUAIWORKER: the AI cluster's nodes
  default = "10.43.11.64/26"
}
variable "nexus_cidr" { # registry.areeba.com
  default = "10.43.17.16/32"
}
# Oracle's name for "all Dubai services" behind the service gateway.
variable "oracle_services" {
  default = "all-dxb-services-in-oracle-services-network"
}
variable "kubernetes_version" {
  default = "v1.36.4"
}
# The newest Oracle Linux 9 x86 OKE image for the version; its OCID is looked up.
variable "node_image_name" {
  default = "Oracle-Linux-9.8-2026.10.16-0-OKE-1.36.4-2067"
}
variable "cluster_name" {
  default = "kubesight-proof"
}
variable "owner" {
  description = "A work email (K17)."
  type        = string
}
variable "expiry" {
  description = "YYYY-MM-DD (K17)."
  type        = string
}

locals {
  tags = {
    "kubesight"         = "proof"
    "kubesight-cluster" = var.cluster_name
    "owner"             = var.owner
    "expiry"            = var.expiry
  }
  # Oracle's standard OKE bootstrap first, then the Nexus name (K16).
  cloud_init = <<-EOT
    #!/bin/bash
    curl --fail -H "Authorization: Bearer Oracle" -L0 http://169.254.169.254/opc/v2/instance/metadata/oke_init_script | base64 --decode >/var/run/oke-init.sh
    bash /var/run/oke-init.sh
    echo '10.43.17.16      registry.areeba.com' >> /etc/hosts
  EOT
  node_image_id = one([
    for source in data.oci_containerengine_node_pool_option.all.sources :
    source.image_id if source.source_name == var.node_image_name
  ])
}

data "oci_containerengine_node_pool_option" "all" {
  node_pool_option_id = "all"
  compartment_id      = var.compartment_id
}

# ---------------------------------------------------------------------------
# K11: two NSGs, Oracle's private-endpoint example, no SSH, no 0.0.0.0/0
# ---------------------------------------------------------------------------

resource "oci_core_network_security_group" "endpoint" {
  compartment_id = var.compartment_id
  vcn_id         = var.vcn_id
  display_name   = "${var.cluster_name}-endpoint"
  freeform_tags  = local.tags
  lifecycle {
    ignore_changes = [defined_tags]
  }
}

resource "oci_core_network_security_group" "workers" {
  compartment_id = var.compartment_id
  vcn_id         = var.vcn_id
  display_name   = "${var.cluster_name}-workers"
  freeform_tags  = local.tags
  lifecycle {
    ignore_changes = [defined_tags]
  }
}

locals {
  # [nsg, direction, protocol, cidr, cidr type, port min, port max, icmp type, description]
  rules = [
    # Endpoint NSG
    ["endpoint", "INGRESS", "6", var.kubesight_source_cidr, "CIDR_BLOCK", 6443, 6443, null, "KubeSight to the API"],
    ["endpoint", "INGRESS", "6", var.worker_subnet_cidr, "CIDR_BLOCK", 6443, 6443, null, "Workers to the API"],
    ["endpoint", "INGRESS", "6", var.worker_subnet_cidr, "CIDR_BLOCK", 12250, 12250, null, "Workers to the control plane"],
    ["endpoint", "INGRESS", "1", var.worker_subnet_cidr, "CIDR_BLOCK", null, null, 3, "Path discovery from the workers"],
    ["endpoint", "INGRESS", "1", var.worker_subnet_cidr, "CIDR_BLOCK", null, null, 4, "Path discovery from the workers"],
    ["endpoint", "EGRESS", "6", var.oracle_services, "SERVICE_CIDR_BLOCK", null, null, null, "OKE to Oracle's services"],
    ["endpoint", "EGRESS", "6", var.worker_subnet_cidr, "CIDR_BLOCK", null, null, null, "Control plane to the workers"],
    ["endpoint", "EGRESS", "1", var.worker_subnet_cidr, "CIDR_BLOCK", null, null, 3, "Path discovery to the workers"],
    ["endpoint", "EGRESS", "1", var.worker_subnet_cidr, "CIDR_BLOCK", null, null, 4, "Path discovery to the workers"],
    # Worker NSG
    ["workers", "INGRESS", "all", var.worker_subnet_cidr, "CIDR_BLOCK", null, null, null, "Pod to pod, node to node"],
    ["workers", "INGRESS", "6", var.endpoint_subnet_cidr, "CIDR_BLOCK", null, null, null, "Control plane to the workers"],
    ["workers", "INGRESS", "1", var.vcn_cidr, "CIDR_BLOCK", null, null, 3, "Path discovery"],
    ["workers", "INGRESS", "1", var.vcn_cidr, "CIDR_BLOCK", null, null, 4, "Path discovery"],
    ["workers", "INGRESS", "6", var.service_lb_subnet_cidr, "CIDR_BLOCK", 30000, 32767, null, "Load balancers to node ports"],
    ["workers", "INGRESS", "6", var.service_lb_subnet_cidr, "CIDR_BLOCK", 10256, 10256, null, "Load balancer health checks"],
    ["workers", "EGRESS", "6", var.endpoint_subnet_cidr, "CIDR_BLOCK", 6443, 6443, null, "Workers to the API"],
    ["workers", "EGRESS", "6", var.endpoint_subnet_cidr, "CIDR_BLOCK", 12250, 12250, null, "Workers to the control plane"],
    ["workers", "EGRESS", "6", var.oracle_services, "SERVICE_CIDR_BLOCK", null, null, null, "Workers to Oracle's services"],
    ["workers", "EGRESS", "all", var.worker_subnet_cidr, "CIDR_BLOCK", null, null, null, "Pod to pod, node to node"],
    ["workers", "EGRESS", "1", var.vcn_cidr, "CIDR_BLOCK", null, null, 3, "Path discovery"],
    ["workers", "EGRESS", "1", var.vcn_cidr, "CIDR_BLOCK", null, null, 4, "Path discovery"],
    ["workers", "EGRESS", "6", var.nexus_cidr, "CIDR_BLOCK", 443, 443, null, "Images from Nexus"],
  ]
  nsg_ids = {
    endpoint = oci_core_network_security_group.endpoint.id
    workers  = oci_core_network_security_group.workers.id
  }
}

resource "oci_core_network_security_group_security_rule" "rule" {
  for_each = { for i, r in local.rules : format("%02d-%s-%s", i, r[0], lower(r[1])) => r }

  network_security_group_id = local.nsg_ids[each.value[0]]
  direction                 = each.value[1]
  protocol                  = each.value[2]
  description               = each.value[8]
  stateless                 = false
  source                    = each.value[1] == "INGRESS" ? each.value[3] : null
  source_type               = each.value[1] == "INGRESS" ? each.value[4] : null
  destination               = each.value[1] == "EGRESS" ? each.value[3] : null
  destination_type          = each.value[1] == "EGRESS" ? each.value[4] : null

  dynamic "tcp_options" {
    for_each = each.value[2] == "6" && each.value[5] != null ? [1] : []
    content {
      destination_port_range {
        min = each.value[5]
        max = each.value[6]
      }
    }
  }
  dynamic "icmp_options" {
    for_each = each.value[2] == "1" ? [1] : []
    content {
      type = each.value[7]
      code = each.value[7] == 3 ? 4 : null
    }
  }
}

# ---------------------------------------------------------------------------
# K6, K7, K8, K10: enhanced, three subnets, private, flannel
# ---------------------------------------------------------------------------

resource "oci_containerengine_cluster" "cluster" {
  compartment_id     = var.compartment_id
  name               = var.cluster_name
  kubernetes_version = var.kubernetes_version
  vcn_id             = var.vcn_id
  type               = "ENHANCED_CLUSTER"

  endpoint_config {
    subnet_id            = var.endpoint_subnet_id
    is_public_ip_enabled = false
    nsg_ids              = [oci_core_network_security_group.endpoint.id]
  }
  cluster_pod_network_options {
    cni_type = "FLANNEL_OVERLAY"
  }
  options {
    service_lb_subnet_ids = [var.service_lb_subnet_id]
    kubernetes_network_config {
      pods_cidr     = "10.244.0.0/16"
      services_cidr = "10.96.0.0/16"
    }
  }
  freeform_tags = local.tags

  lifecycle {
    ignore_changes = [
      defined_tags,
      options[0].persistent_volume_config[0].defined_tags,
      options[0].service_lb_config[0].defined_tags,
    ]
  }
}

# K9: E6 only; 2 nodes of 2 OCPUs and 16 GB fill the test quota.
resource "oci_containerengine_node_pool" "workers" {
  cluster_id         = oci_containerengine_cluster.cluster.id
  compartment_id     = var.compartment_id
  name               = "${var.cluster_name}-workers"
  kubernetes_version = var.kubernetes_version
  node_shape         = "VM.Standard.E6.Flex"

  node_shape_config {
    ocpus         = 2
    memory_in_gbs = 16
  }
  node_source_details {
    source_type             = "IMAGE"
    image_id                = local.node_image_id
    boot_volume_size_in_gbs = "50"
  }
  node_config_details {
    size    = 2
    nsg_ids = [oci_core_network_security_group.workers.id]
    placement_configs {
      availability_domain = var.availability_domain
      subnet_id           = var.worker_subnet_id
    }
    node_pool_pod_network_option_details {
      cni_type = "FLANNEL_OVERLAY"
    }
    freeform_tags = local.tags
  }
  node_metadata = {
    user_data = base64encode(local.cloud_init)
  }
  freeform_tags = local.tags

  lifecycle {
    ignore_changes = [defined_tags, node_config_details[0].defined_tags]
  }
}

output "cluster_id" {
  value = oci_containerengine_cluster.cluster.id
}
output "private_endpoint" {
  value = try(oci_containerengine_cluster.cluster.endpoints[0].private_endpoint, null)
}
output "node_pool_id" {
  value = oci_containerengine_node_pool.workers.id
}
output "node_image_id" {
  value = local.node_image_id
}
