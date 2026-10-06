FROM python:3.12-slim

WORKDIR /app

ARG KUBECTL_VERSION=v1.32.2
ARG HELM_VERSION=v3.17.2

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates git \
    && curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl \
    && curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" | tar xz -C /tmp \
    && mv /tmp/linux-amd64/helm /usr/local/bin/helm \
    && rm -rf /tmp/linux-amd64 \
    && rm -rf /var/lib/apt/lists/*

# OpenTofu creates the Cluster Builder's VMware VMs. The binary is checked
# against the release's SHA256SUMS, and the vSphere provider is mirrored into
# the image (`tofu providers mirror` verifies its checksums) so provisioning
# never downloads anything at runtime — the generated CLI config installs that
# provider from this mirror only. Keep VSPHERE_PROVIDER_VERSION in step with
# tofu_config.PROVIDER_VERSION.
ARG TOFU_VERSION=1.13.1
ARG VSPHERE_PROVIDER_VERSION=2.17.1
ENV KUBESIGHT_TOFU_PROVIDER_MIRROR=/opt/kubesight/tofu/providers \
    KUBESIGHT_TOFU_VSPHERE_PROVIDER_VERSION=${VSPHERE_PROVIDER_VERSION} \
    CHECKPOINT_DISABLE=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends unzip \
    && cd /tmp \
    && curl -fsSLO "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_amd64.zip" \
    && curl -fsSLO "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_SHA256SUMS" \
    && grep " tofu_${TOFU_VERSION}_linux_amd64.zip$" "tofu_${TOFU_VERSION}_SHA256SUMS" | sha256sum -c - \
    && unzip -o "tofu_${TOFU_VERSION}_linux_amd64.zip" tofu -d /usr/local/bin \
    && chmod +x /usr/local/bin/tofu \
    && mkdir -p /tmp/tofu-mirror "${KUBESIGHT_TOFU_PROVIDER_MIRROR}" \
    && printf 'terraform {\n  required_providers {\n    vsphere = {\n      source  = "vmware/vsphere"\n      version = "= %s"\n    }\n  }\n}\n' "${VSPHERE_PROVIDER_VERSION}" > /tmp/tofu-mirror/main.tf \
    && (cd /tmp/tofu-mirror && tofu providers mirror -platform=linux_amd64 "${KUBESIGHT_TOFU_PROVIDER_MIRROR}") \
    && rm -rf /tmp/tofu-mirror /tmp/tofu_* \
    && apt-get purge -y unzip && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# Windows CRLF breaks shebang — normalize and invoke via sh explicitly
RUN sed -i 's/\r$//' /app/k8s_entrypoint.sh && chmod +x /app/k8s_entrypoint.sh

ENV PYTHONUNBUFFERED=1
EXPOSE 5000

# For plain Docker/compose runs; Kubernetes ignores this and uses its probes.
# The start period covers boot-time migrations.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3   CMD curl -fsS http://127.0.0.1:5000/health > /dev/null || exit 1

ENTRYPOINT ["sh", "/app/k8s_entrypoint.sh"]
