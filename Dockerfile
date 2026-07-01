FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates tar \
    && rm -rf /var/lib/apt/lists/*

# Install tdl binary. Pin to a known-good version.
ARG TDL_VERSION=0.20.2
RUN set -eux; \
    arch="$(uname -m)"; \
    case "$arch" in \
        x86_64) pkg="tdl_Linux_64bit.tar.gz" ;; \
        aarch64) pkg="tdl_Linux_arm64.tar.gz" ;; \
        *) echo "unsupported arch $arch" && exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/iyear/tdl/releases/download/v${TDL_VERSION}/${pkg}" -o /tmp/tdl.tgz; \
    mkdir -p /tmp/tdl && tar -xzf /tmp/tdl.tgz -C /tmp/tdl; \
    install -m 0755 /tmp/tdl/tdl /usr/local/bin/tdl; \
    rm -rf /tmp/tdl /tmp/tdl.tgz

# Install uv and project deps
RUN pip install --no-cache-dir uv
WORKDIR /app

COPY pyproject.toml README.md ./
# Use `uv sync` to resolve and install into a project-local .venv
# For a container we install system-wide via uv pip:
COPY . .
# Runtime deps only — the dev extra (pytest/ruff/mypy) has no place in the image.
RUN uv pip install --system "."

ENV WEB_HOST=0.0.0.0 \
    WEB_PORT=8765 \
    DATA_DIR=/state \
    EXPORT_DIR=/exports \
    TDL_BINARY_PATH=/usr/local/bin/tdl

VOLUME ["/state", "/exports"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -sf http://127.0.0.1:8765/health || exit 1

CMD ["python", "-m", "src.main"]
