#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install --no-install-recommends -y \
  build-essential bison flex bzip2 ca-certificates curl file pkg-config wget \
  libwebkit2gtk-4.1-dev libayatana-appindicator3-dev librsvg2-dev \
  libssl-dev libxdo-dev patchelf squashfs-tools \
  libldap2-dev libsasl2-dev libpq-dev libmagic1 libffi-dev zlib1g-dev \
  libjpeg-turbo8 libxrender1 libxext6 fontconfig fonts-dejavu-core
