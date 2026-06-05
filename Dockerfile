ARG TARGETPLATFORM
FROM --platform=$TARGETPLATFORM mysterysd/wzmlx:v3

WORKDIR /usr/src/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends mediainfo mktorrent \
    && rm -rf /var/lib/apt/lists/*

RUN chmod 777 /usr/src/app
RUN uv venv /usr/src/app/.venv --system-site-packages
ENV VIRTUAL_ENV=/usr/src/app/.venv
ENV PATH="/usr/src/app/.venv/bin:$PATH"

COPY requirements.txt .
RUN uv pip install --python /usr/src/app/.venv/bin/python --no-cache-dir -r requirements.txt

COPY . .

CMD ["bash", "start.sh"]
