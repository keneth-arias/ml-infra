FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535
COPY --from=tools /terraform /nebius /usr/local/bin/
RUN echo 'dbfef0be43ba2117481be510d3b83a2d8cdfa6ae7ec69c5aeb04b3947956ff7e  /usr/local/bin/terraform' | sha256sum -c - && echo '213babf6eac0137afc9b5a61c20dd298aebd049caf006e43d326036516b157af  /usr/local/bin/nebius' | sha256sum -c -
COPY --from=tools /mirror /mirror
WORKDIR /opt/cleanup
COPY management/kind/s3-requirements.txt management/otlp-requirements.txt /tmp/
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/s3-requirements.txt -r /tmp/otlp-requirements.txt && pip check
COPY main.tf versions.tf .terraform.lock.hcl expire-workers worker_cleanup.py with-credentials ./
COPY management /opt/cleanup/management
ENV TF_CLI_CONFIG_FILE=/opt/cleanup/management/kind/terraform.rc TF_IN_AUTOMATION=1 CHECKPOINT_DISABLE=1 PYTHONDONTWRITEBYTECODE=1 AI_AGENT=worker-cleanup
RUN terraform init -backend=false -lockfile=readonly && rm -rf .terraform
ARG BUILD_COMMIT
LABEL org.opencontainers.image.revision=$BUILD_COMMIT
USER 1000:1000
ENTRYPOINT ["python3","-B","/opt/cleanup/management/kind/tick.py"]
