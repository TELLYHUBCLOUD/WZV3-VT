FROM python:3.12-slim-bookworm AS nllb-builder

RUN python -m pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        "torch==2.12.1" \
    && python -m pip install --no-cache-dir \
        "ctranslate2==4.8.1" \
        "transformers<5" \
        sentencepiece \
    && ct2-transformers-converter \
        --model facebook/nllb-200-distilled-600M \
        --output_dir /opt/models/nllb-200-distilled-600M-int8 \
        --quantization int8 \
        --copy_files tokenizer.json tokenizer_config.json sentencepiece.bpe.model special_tokens_map.json

FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    VIRTUAL_ENV=/wzvenv \
    PATH="/wzvenv/bin:${PATH}"

WORKDIR /usr/src/app

RUN sed -i 's/Components: main/Components: main contrib non-free/g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        aria2 \
        bash \
        build-essential \
        ca-certificates \
        coreutils \
        cpulimit \
        curl \
        ffmpeg \
        fontconfig \
        fonts-dejavu-core \
        git \
        imagemagick \
        jq \
        libffi-dev \
        libmagic1 \
        libssl-dev \
        libxml2-dev \
        libxslt1-dev \
        mediainfo \
        mktorrent \
        netcat-openbsd \
        nodejs \
        p7zip-full \
        par2 \
        pkg-config \
        procps \
        qbittorrent-nox \
        rclone \
        sabnzbdplus \
        tini \
        tzdata \
        unrar-free \
        unzip \
        util-linux \
        zlib1g-dev \
        zip \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /wzvenv \
    && python -m pip install --no-cache-dir --upgrade pip uv setuptools wheel

COPY requirements.txt .
RUN uv pip install --python /wzvenv/bin/python --no-cache -r requirements.txt \
    && apt-get purge -y --auto-remove \
        build-essential \
        libffi-dev \
        libssl-dev \
        libxml2-dev \
        libxslt1-dev \
        pkg-config \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/* /root/.cache

COPY --from=nllb-builder /opt/models/nllb-200-distilled-600M-int8 /opt/models/nllb-200-distilled-600M-int8
RUN test -s /opt/models/nllb-200-distilled-600M-int8/model.bin \
    && test -s /opt/models/nllb-200-distilled-600M-int8/config.json \
    && test -s /opt/models/nllb-200-distilled-600M-int8/tokenizer_config.json

COPY . .
RUN chmod +x start.sh setpkgs.sh

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--", "bash", "start.sh"]
