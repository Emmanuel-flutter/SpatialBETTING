import os
from dotenv import load_dotenv

load_dotenv()

CONFLUENT_API_KEY = os.getenv("CONFLUENT_API_KEY")
CONFLUENT_API_SECRET = os.getenv("CONFLUENT_API_SECRET")
CONFLUENT_BOOTSTRAP_SERVER = os.getenv("CONFLUENT_BOOTSTRAP_SERVER")
CONFLUENT_CLUSTER_ID = os.getenv("CONFLUENT_CLUSTER_ID")
CONFLUENT_ENV_ID = os.getenv("CONFLUENT_ENV_ID")
CONFLUENT_TOPIC = os.getenv("CONFLUENT_TOPIC", "trading.nxt.opta.soccer.raw")

KAFKA_CONSUMER_CONFIG = {
    "bootstrap.servers": CONFLUENT_BOOTSTRAP_SERVER,
    "security.protocol": "SASL_SSL",
    "sasl.mechanisms": "PLAIN",
    "sasl.username": CONFLUENT_API_KEY,
    "sasl.password": CONFLUENT_API_SECRET,
    "session.timeout.ms": 45000,
}
