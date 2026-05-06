FROM python:3.11-slim

# nodejs: lets Claude SessionStart hooks (from mounted ~/.claude) execute
# instead of failing — saves ~1-2s per chat turn.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git openssh-client \
    netcat-openbsd \
    nodejs \
    && rm -rf /var/lib/apt/lists/*
# netcat-openbsd: enables SSH ProxyCommand support for HTTP/SOCKS proxies

WORKDIR /app

# Build-time deps for the install (hatchling)
RUN pip install --no-cache-dir hatchling

# Copy package source + install. The `[prod]` extra brings in gunicorn.
COPY pyproject.toml README.md ./
COPY oh_my_skill/ oh_my_skill/
RUN pip install --no-cache-dir ".[prod]"

# Symlink CLI helpers onto PATH so the chat workspace can call them.
RUN ln -sf /usr/local/lib/python3.11/site-packages/oh_my_skill/cli_helpers/oms-save  /usr/local/bin/oms-save  && \
    ln -sf /usr/local/lib/python3.11/site-packages/oh_my_skill/cli_helpers/oms-show  /usr/local/bin/oms-show  && \
    ln -sf /usr/local/lib/python3.11/site-packages/oh_my_skill/cli_helpers/oms-tag   /usr/local/bin/oms-tag   && \
    ln -sf /usr/local/lib/python3.11/site-packages/oh_my_skill/cli_helpers/oms-untag /usr/local/bin/oms-untag && \
    ln -sf /usr/local/lib/python3.11/site-packages/oh_my_skill/cli_helpers/oms-list  /usr/local/bin/oms-list  && \
    chmod +x /usr/local/lib/python3.11/site-packages/oh_my_skill/cli_helpers/oms-*

ENV OMI_DATA_DIR=/data \
    SETTINGS_DB=/data/oh-my-skill.db \
    SKILLCARDS_DB=/data/skillcards.db \
    SKILLS_DIR=/skills \
    PORT=80

EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost/healthz || exit 1

CMD ["oh-my-skill", "--host", "0.0.0.0", "--port", "80", "--no-browser"]
