FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535
RUN apt-get update && apt-get install -y --no-install-recommends openssh-server openssh-client rsync procps socat netcat-openbsd util-linux tini && rm -rf /var/lib/apt/lists/* /etc/ssh/ssh_host_* && mkdir -p /run/sshd
COPY requirements.txt /opt/sky-cpu/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip pip install --require-hashes --only-binary=:all: --no-deps -r /opt/sky-cpu/requirements.txt && pip check
RUN groupadd --gid 1000 sky && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash --password '*' sky
COPY sshd_config start-worker /opt/sky-cpu/
ENV HOME=/home/sky RAY_USAGE_STATS_ENABLED=0 PYTHONUNBUFFERED=1
WORKDIR /home/sky
USER 1000:1000
