# One Dockerfile, one image per service:
#   docker build --build-arg SERVICE=ingest -t envbi/ingest:1.0 .
FROM python:3.12-slim

ARG SERVICE
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/common

WORKDIR /app
COPY services/${SERVICE}/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY common /app/common
COPY services/${SERVICE} /app/service
WORKDIR /app/service

RUN useradd --uid 10001 --no-create-home app
USER 10001

# Overridden per service in compose / k8s.
CMD ["python", "-c", "print('set a command')"]
