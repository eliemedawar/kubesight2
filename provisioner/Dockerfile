# KubeSight provisioner image.
#
# One image for two uses on the AI cluster (OCIDUAI-K8S):
#   * the OKE proof pods (deploy/ai-cluster/proof), and
#   * the Jobs KubeSight launches to plan, apply and bind OKE clusters (K3).
#
# It carries OpenTofu, the oracle/oci provider in a filesystem mirror with its
# lock file, the OCI CLI (OKE workload identity, OKE tokens) and kubectl. Nothing
# is downloaded when it runs. Every download here is checked against its
# publisher's checksum. Build it where the internet is reachable (the Bitbucket
# image step does) and push it to registry.areeba.com; the cluster pulls it by
# digest.
FROM python:3.12-slim-bookworm

# OpenTofu: the newest 1.13 release (1.13.1 has advisories fixed later in 1.13).
ARG TOFU_VERSION=1.13.9
# Keep in step with TOFU_PROVIDER in
# backend/api/services/cluster_build/provisioning/clouds/oci.py.
ARG OCI_PROVIDER_VERSION=9.11.0
ARG OCI_CLI_VERSION=3.96.0
ARG KUBECTL_VERSION=v1.36.6

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TF_CLI_CONFIG_FILE=/etc/tofu/tofurc \
    TF_IN_AUTOMATION=1 \
    TF_INPUT=0 \
    CHECKPOINT_DISABLE=1 \
    OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING=True \
    HOME=/work

RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && cd /tmp \
    && curl -fsSLO "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_amd64.zip" \
    && curl -fsSLO "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_SHA256SUMS" \
    && grep " tofu_${TOFU_VERSION}_linux_amd64.zip$" "tofu_${TOFU_VERSION}_SHA256SUMS" | sha256sum -c - \
    && unzip -o "tofu_${TOFU_VERSION}_linux_amd64.zip" tofu -d /usr/local/bin \
    && chmod 0755 /usr/local/bin/tofu \
    && curl -fsSLO "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && curl -fsSLO "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl.sha256" \
    && echo "$(cat kubectl.sha256)  kubectl" | sha256sum -c - \
    && install -m 0755 kubectl /usr/local/bin/kubectl \
    && rm -rf /tmp/* \
    && rm -rf /var/lib/apt/lists/*

RUN pip install "oci-cli==${OCI_CLI_VERSION}" \
    && oci --version

# Readable by the Job's uid whatever mode the checkout gave it (KubeSight CI's
# checkout made it owner-only, and OpenTofu then went to the internet).
COPY --chmod=0644 tofurc /etc/tofu/tofurc
RUN chmod 0755 /etc/tofu

# The oracle/oci provider in an UNPACKED filesystem mirror (tofu init links it
# into the working folder instead of copying ~400 MB), and the lock file
# OpenTofu checks it against. `tofu providers mirror` verifies the provider zip
# against the registry's signed checksums before anything is unpacked.
RUN mkdir -p /tmp/mirror /opt/tofu/providers /opt/kubesight/tofu \
    && cd /opt/kubesight/tofu \
    && printf '{"terraform":{"required_providers":{"oci":{"source":"oracle/oci","version":"= %s"}}}}\n' \
       "${OCI_PROVIDER_VERSION}" > versions.tf.json \
    && TF_CLI_CONFIG_FILE=/dev/null tofu providers mirror -platform=linux_amd64 /tmp/mirror \
    && target=/opt/tofu/providers/registry.opentofu.org/oracle/oci/${OCI_PROVIDER_VERSION}/linux_amd64 \
    && mkdir -p "$target" \
    && unzip -o /tmp/mirror/registry.opentofu.org/oracle/oci/terraform-provider-oci_${OCI_PROVIDER_VERSION}_linux_amd64.zip -d "$target" \
    && tofu providers lock -platform=linux_amd64 -fs-mirror=/opt/tofu/providers \
    && rm versions.tf.json \
    && test -s .terraform.lock.hcl \
    && rm -rf /tmp/mirror \
    && apt-get purge -y unzip && apt-get autoremove -y

COPY kubesight_provision.py /usr/local/bin/kubesight-provision
# The OKE proof (deploy/ai-cluster/proof): OpenTofu config and step scripts.
COPY proof/ /opt/kubesight/proof/

RUN sed -i 's/\r$//' /usr/local/bin/kubesight-provision /opt/kubesight/proof/* \
    && chmod 0555 /usr/local/bin/kubesight-provision /opt/kubesight/proof/*.sh \
    && chmod 0444 /opt/kubesight/proof/main.tf /opt/kubesight/proof/step4.yaml /opt/kubesight/proof/values.sh \
    && groupadd --gid 10001 provisioner \
    && useradd --uid 10001 --gid 10001 --home-dir /work --no-create-home --shell /usr/sbin/nologin provisioner \
    && mkdir -p /work \
    && chown 10001:10001 /work

USER 10001:10001
WORKDIR /work

# Fail the build, not the first cluster: the Job's uid must read the CLI
# config, the lock file and the provider in the mirror.
RUN test -r /etc/tofu/tofurc     && test -r /opt/kubesight/tofu/.terraform.lock.hcl     && test -x /opt/tofu/providers/registry.opentofu.org/oracle/oci/${OCI_PROVIDER_VERSION}/linux_amd64/terraform-provider-oci_v${OCI_PROVIDER_VERSION}     && tofu version

# The proof pods override this with `sleep`; KubeSight's Jobs run it as is.
ENTRYPOINT ["/usr/local/bin/kubesight-provision"]
