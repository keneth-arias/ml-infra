FROM python:3.11-slim-bookworm
RUN pip install --no-cache-dir mlflow==3.18.0 psycopg2-binary==2.9.11 boto3==1.42.40
USER 1000:1000
ENV HOME=/tmp
ENTRYPOINT ["mlflow"]
