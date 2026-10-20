"""Environment-driven settings shared by every service.

Each value can be overridden with an environment variable of the same name,
which is how the Docker Compose file and the Kubernetes ConfigMap configure
the services.
"""
import os
import socket


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


MQTT_HOST = _env("MQTT_HOST", "localhost")
MQTT_PORT = int(_env("MQTT_PORT", "1883"))
# Shared subscription so ingest replicas split the load instead of each
# receiving every message.
MQTT_TOPIC = _env("MQTT_TOPIC", "$share/ingest/sensors/#")
MQTT_QOS = int(_env("MQTT_QOS", "1"))

REDIS_URL = _env("REDIS_URL", "redis://localhost:6379/0")
RAW_STREAM = _env("RAW_STREAM", "telemetry:raw")
DLQ_STREAM = _env("DLQ_STREAM", "telemetry:dlq")
CONSUMER_GROUP = _env("CONSUMER_GROUP", "processors")
# Replay window: the raw stream keeps roughly this many entries.
STREAM_MAXLEN = int(_env("STREAM_MAXLEN", "500000"))

DATABASE_URL = _env("DATABASE_URL", "postgresql://envbi:envbi@localhost:5432/envbi")
# The gateway only reads, so in Kubernetes it points at the streaming replica.
READ_DATABASE_URL = _env("READ_DATABASE_URL", DATABASE_URL)

BATCH_SIZE = int(_env("BATCH_SIZE", "500"))
BLOCK_MS = int(_env("BLOCK_MS", "1000"))
# A pending message idle for longer than this is assumed to belong to a dead
# worker and is claimed by a live one.
CLAIM_IDLE_MS = int(_env("CLAIM_IDLE_MS", "30000"))
# Missing fields are forward-filled from the last good reading if it is newer
# than this.
GAP_FILL_MAX_AGE_S = float(_env("GAP_FILL_MAX_AGE_S", "300"))
ANOMALY_Z = float(_env("ANOMALY_Z", "4.0"))
ANOMALY_ALPHA = float(_env("ANOMALY_ALPHA", "0.05"))
ANOMALY_WARMUP = int(_env("ANOMALY_WARMUP", "30"))

METRICS_PORT = int(_env("METRICS_PORT", "9100"))
HOSTNAME = _env("HOSTNAME", socket.gethostname())

POWERBI_PUSH_URL = _env("POWERBI_PUSH_URL", "")
POWERBI_PUSH_INTERVAL_S = float(_env("POWERBI_PUSH_INTERVAL_S", "5"))
