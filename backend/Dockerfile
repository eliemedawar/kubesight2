# syntax=docker/dockerfile:1.7
FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

ARG TARGETARCH
ARG KUBECTL_VERSION=v1.36.3
ARG HELM_VERSION=v3.20.2

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/kubesight \
    KUBECONFIG=/home/kubesight/.kube/config

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && case "${TARGETARCH}" in \
         amd64) KUBECTL_SHA=ebbd080e7c2e275093b55915722043257eb24004363e20acb3c4d71919f88336; HELM_SHA=258e830a9e613c8a7a302d6059b4bb3b9758f2f3e1bb8ea0d707ce10a9a72fea ;; \
         arm64) KUBECTL_SHA=3d86f24401c41ae5a46ac50eef8865fe891d3647d324a0836f6c63757a126e62; HELM_SHA=5ea2d6bc2cda3f8edf985e028809f5a9278f404fb8ab24044de9b7cb9b79a691 ;; \
         *) echo "unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
       esac \
    && curl --fail --show-error --location --proto '=https' --tlsv1.2 \
         "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
         --output /tmp/kubectl \
    && echo "${KUBECTL_SHA}  /tmp/kubectl" | sha256sum --check --strict \
    && install -m 0755 /tmp/kubectl /usr/local/bin/kubectl \
    && curl --fail --show-error --location --proto '=https' --tlsv1.2 \
         "https://get.helm.sh/helm-${HELM_VERSION}-linux-${TARGETARCH}.tar.gz" \
         --output /tmp/helm.tar.gz \
    && echo "${HELM_SHA}  /tmp/helm.tar.gz" | sha256sum --check --strict \
    && tar xzf /tmp/helm.tar.gz -C /tmp \
    && install -m 0755 "/tmp/linux-${TARGETARCH}/helm" /usr/local/bin/helm \
    && rm -rf /tmp/kubectl /tmp/helm.tar.gz "/tmp/linux-${TARGETARCH}" /var/lib/apt/lists/* \
    && groupadd --gid 10001 kubesight \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/kubesight kubesight \
    && mkdir -p /home/kubesight/.kube /var/lib/kubesight/artifacts \
    && chown -R 10001:10001 /home/kubesight /var/lib/kubesight

COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --no-compile -r requirements.txt

COPY --chown=10001:10001 . .

USER 10001:10001
EXPOSE 5000

CMD ["gunicorn", "--workers", "2", "--threads", "4", "--bind", "0.0.0.0:5000", "--timeout", "300", "api:create_app()"]
