# syntax=docker/dockerfile:1.6
# ============================================================================
# Builder image — runs ``flatpak-builder`` for manifest sources.
#
# Kept apart from the worker on purpose: it executes third-party build
# recipes, so it gets no database credentials and no signing key — only
# redis (job queue) and the shared /data/builder volume. It needs user
# namespaces for bubblewrap; see docker-compose.yml for the security_opt.
# ============================================================================
ARG API_IMAGE=wildcat-hub-backend:latest
FROM ${API_IMAGE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
        flatpak-builder \
        git \
        ccache \
        patch \
        bzip2 \
        xz-utils \
        unzip \
        zstd \
        elfutils \
        # appstreamcli compose renders icons through gdk-pixbuf: without
        # these loaders every SVG/PNG icon is a "file-read-error".
        librsvg2-common \
        libgdk-pixbuf2.0-bin \
        libgdk-pixbuf-2.0-0 \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* /usr/share/doc /usr/share/man
RUN mkdir -p /data/builder && chown -R fdroid:fdroid /data
COPY app /app/app
RUN /opt/venv/bin/python -m compileall -q /app/app || true
USER fdroid:fdroid
ENV FLATPAK_USER_DIR=/data/builder/flatpak
CMD ["python", "-m", "arq", "app.workers.build_tasks.BuilderSettings"]
