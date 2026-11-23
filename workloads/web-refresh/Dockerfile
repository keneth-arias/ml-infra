# La base está importada solo en bm. Verificar su ID antes de construir (README).
FROM local/airaware-fresh-peak-collector:synthetic-qualified
USER root
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --uid 1000 --user-group --no-create-home --home-dir /tmp airaware
USER 1000:1000
ENTRYPOINT ["/bin/sh"]
