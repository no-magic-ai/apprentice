# Apprentice sandbox image: the only runtime that executes generated code and renders scenes.
#
# The operator builds it explicitly (apprentice never pulls, builds or installs anything at
# runtime) and configures the resulting image ID in [execution].image. The build binds this
# recipe's digest into the image so a stale image is refused after the recipe changes; see
# docs/configuration.md#execution for the exact build commands.
#
# Inputs are pinned: both base images by manifest digest, Debian packages to one
# snapshot.debian.org date and every Python distribution by version and hash in
# requirements.lock (regenerate with the command recorded in its header).

FROM ghcr.io/astral-sh/uv:0.6.14@sha256:3362a526af7eca2fcd8604e6a07e873fb6e4286d8837cb753503558ce1213664 AS uv

FROM python:3.13.14-slim-bookworm@sha256:67a1e1f215ccda113cfc024e8639049257e88f273898f595b61476d128d387e8

ARG APPRENTICE_RECIPE_SHA256
RUN printf '%s' "$APPRENTICE_RECIPE_SHA256" | grep -Eqx '[0-9a-f]{64}' \
    || { echo "APPRENTICE_RECIPE_SHA256 must be the 64-hex recipe digest" >&2; exit 1; }
LABEL ai.no-magic.apprentice.sandbox-recipe="${APPRENTICE_RECIPE_SHA256}"

RUN rm -f /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' \
        'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20261001T000000Z bookworm main' \
        'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20261001T000000Z bookworm-updates main' \
        'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/20261001T000000Z bookworm-security main' \
        > /etc/apt/sources.list \
    && apt-get update \
    && apt-get install --no-install-recommends -y \
        gcc g++ pkg-config libcairo2-dev libpango1.0-dev \
        ffmpeg fontconfig fonts-dejavu-core \
        texlive-latex-base texlive-latex-extra texlive-fonts-recommended dvisvgm \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /usr/local/bin/uv
COPY requirements.lock /tmp/requirements.lock
RUN uv pip install --system --require-hashes --no-deps -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock /usr/local/bin/uv \
    && rm -rf /root/.cache

ENV PYTHONDONTWRITEBYTECODE=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 HOME=/tmp MPLCONFIGDIR=/tmp
USER 65532:65532
WORKDIR /input
