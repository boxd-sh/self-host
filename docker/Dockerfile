# The tools the boxd installer drives, pinned — and nothing else: no
# installer code, no secrets. ./boxd-install mounts the repo at /work and runs
# ./install.py in here, so an operator needs only Docker.
#
# Pins match what the installer is tested with. To change one: edit it here,
# bump docker/VERSION (the image tag ./boxd-install pulls), merge; CI
# publishes ghcr.io/azin-tech/boxd-installer-tools:<VERSION> for amd64+arm64.
FROM python:3.12.12-slim-bookworm
# Links the ghcr package to the repo: repo admins administer it, and its
# page shows where it's built from.
LABEL org.opencontainers.image.source="https://github.com/azin-tech/boxd-installer" \
      org.opencontainers.image.description="Tools the boxd self-host installer runs with (./boxd-install)"

ARG TARGETARCH
ARG TERRAFORM_VERSION=1.16.4
ARG ANSIBLE_CORE_VERSION=2.21.4
ARG AWSCLI_VERSION=2.37.10
ARG SSM_PLUGIN_VERSION=1.2.707.0
ARG ANSIBLE_POSIX_VERSION=2.2.2
ARG NUMPY_VERSION=2.3.3
ARG GCLOUD_VERSION=512.0.0
ARG GCLOUD_SHA256_amd64=59dab282f72e00b05d8ae47232fdf46765ad6e699cfc70c0a87596e7524dbe68
ARG GCLOUD_SHA256_arm64=48590e13200b3a10567e89316c822f2ec2cf1de624f414449690b67521bf1a97

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl unzip openssh-client openssl git less nano vim-tiny \
    && rm -rf /var/lib/apt/lists/*

# terraform, checked against HashiCorp's SHA256SUMS
RUN set -eux; cd /tmp; \
    zip="terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH}.zip"; \
    curl -fsSLO "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/${zip}"; \
    curl -fsSL "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_SHA256SUMS" \
      | grep " ${zip}\$" | sha256sum -c -; \
    unzip -q "$zip" terraform -d /usr/local/bin; rm "$zip"

# ansible: ansible.builtin plus ansible.posix (mount, sysctl in the volume,
# proxy and worker roles), on the default collections path
# gcloud's own python: a venv holding only numpy, which its IAP tunnel (every
# node ssh) warns about on each call without. gcloud sees site-packages only
# with CLOUDSDK_PYTHON_SITEPACKAGES=1 (ENV below); a venv keeps that from
# also exposing ansible's packages to it.
RUN python3 -m venv /opt/gcloud-python \
    && /opt/gcloud-python/bin/pip install --no-cache-dir "numpy==${NUMPY_VERSION}"
RUN pip install --no-cache-dir "ansible-core==${ANSIBLE_CORE_VERSION}" \
    && ansible-galaxy collection install -p /usr/share/ansible/collections \
       "ansible.posix:==${ANSIBLE_POSIX_VERSION}"

# aws CLI v2 + the Session Manager plugin (all AWS node access is SSM)
RUN set -eux; cd /tmp; \
    case "$TARGETARCH" in amd64) a=x86_64; s=ubuntu_64bit ;; arm64) a=aarch64; s=ubuntu_arm64 ;; *) exit 1 ;; esac; \
    curl -fsSL -o awscli.zip "https://awscli.amazonaws.com/awscli-exe-linux-${a}-${AWSCLI_VERSION}.zip"; \
    unzip -q awscli.zip; ./aws/install; rm -rf aws awscli.zip; \
    curl -fsSL -o ssm.deb "https://s3.amazonaws.com/session-manager-downloads/plugin/${SSM_PLUGIN_VERSION}/${s}/session-manager-plugin.deb"; \
    dpkg -i ssm.deb; rm ssm.deb

# gcloud (GCP clusters: IAP tunnels, GCS state, the GTS EAB key). From
# storage.googleapis.com, not dl.google.com: the latter stalls on some links.
# The tarball is a complete install; install.sh would only call home for the
# component list, so it's skipped.
RUN set -eux; cd /tmp; \
    case "$TARGETARCH" in amd64) a=x86_64; sum="$GCLOUD_SHA256_amd64" ;; arm64) a=arm; sum="$GCLOUD_SHA256_arm64" ;; *) exit 1 ;; esac; \
    curl -fsSL -o gcloud.tgz "https://storage.googleapis.com/cloud-sdk-release/google-cloud-cli-${GCLOUD_VERSION}-linux-${a}.tar.gz"; \
    echo "${sum}  gcloud.tgz" | sha256sum -c -; \
    tar -xzf gcloud.tgz -C /opt; rm gcloud.tgz; \
    CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK=true /opt/google-cloud-sdk/bin/gcloud version

# Runs as the caller's uid (files in the mounted repo stay theirs). ssh
# refuses a uid without a passwd entry, so the entrypoint adds one.
RUN mkdir -p /home/installer && chmod 1777 /home/installer && chmod 0666 /etc/passwd
COPY entrypoint.sh /usr/local/bin/boxd-entrypoint
ENV PATH=/opt/google-cloud-sdk/bin:$PATH \
    HOME=/home/installer \
    EDITOR=nano \
    CLOUDSDK_CORE_DISABLE_USAGE_REPORTING=true \
    CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK=true \
    CHECKPOINT_DISABLE=1 \
    CLOUDSDK_PYTHON=/opt/gcloud-python/bin/python \
    CLOUDSDK_PYTHON_SITEPACKAGES=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/boxd-entrypoint"]
CMD ["./install.py", "--help"]
