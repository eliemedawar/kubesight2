# Android re-signing job image.
#
# Runs as a short-lived Kubernetes Job: KubeSight hands it one unsigned binary
# and it hands back a signed one. Debian ships apksigner and zipalign as
# packages, so the Android SDK does not have to be downloaded or licensed here.
#
# jarsigner comes from the JDK (the JRE does not include it) and is the only
# tool that can sign an .aab — apksigner is APK-only.
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        openjdk-17-jdk-headless \
        apksigner \
        zipalign \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY sign.sh /usr/local/bin/sign.sh
RUN chmod 0755 /usr/local/bin/sign.sh

# Matches the runAsUser KubeSight sets on the Job's pod spec.
RUN groupadd -g 65532 signer && useradd -u 65532 -g 65532 -m -s /usr/sbin/nologin signer
USER 65532:65532

ENTRYPOINT ["/usr/local/bin/sign.sh"]
