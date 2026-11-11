# Local build environment matching the GitHub Actions Ubuntu runner.
# Build from the repository root; the source is bind-mounted at runtime.
FROM node:24-bookworm-slim AS node
FROM ghcr.io/astral-sh/uv:latest AS uv
FROM ubuntu:24.04

COPY --from=node /usr/local/ /usr/local/
COPY --from=uv /uv /uvx /usr/local/bin/
COPY .github/scripts/install-linux-deps.sh /tmp/install-linux-deps.sh
RUN bash /tmp/install-linux-deps.sh \
    && rm -rf /var/lib/apt/lists/* /tmp/install-linux-deps.sh \
    && npm install --global pnpm@12.3.4

ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo
ENV PATH="/opt/cargo/bin:${PATH}"
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs -o /tmp/rustup-init.sh \
    && sh /tmp/rustup-init.sh -y --profile minimal --default-toolchain stable --no-modify-path \
    && rm /tmp/rustup-init.sh \
    && chmod -R a+rwX /opt/cargo /opt/rustup

WORKDIR /workspace
CMD ["bash", ".github/scripts/build-appimage.sh"]
