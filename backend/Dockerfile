FROM python:3.12-slim

WORKDIR /app

ARG KUBECTL_VERSION=v1.32.2
ARG HELM_VERSION=v3.17.2

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl \
    && curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" | tar xz -C /tmp \
    && mv /tmp/linux-amd64/helm /usr/local/bin/helm \
    && rm -rf /tmp/linux-amd64 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# Windows CRLF breaks shebang — normalize and invoke via sh explicitly
RUN sed -i 's/\r$//' /app/k8s_entrypoint.sh && chmod +x /app/k8s_entrypoint.sh

ENV PYTHONUNBUFFERED=1
EXPOSE 5000

ENTRYPOINT ["sh", "/app/k8s_entrypoint.sh"]
