import os
from dotenv import load_dotenv

load_dotenv()

CONFLUENT_API_KEY = os.getenv("CONFLUENT_API_KEY")
CONFLUENT_API_SECRET = os.getenv("CONFLUENT_API_SECRET")
CONFLUENT_BOOTSTRAP_SERVER = os.getenv("CONFLUENT_BOOTSTRAP_SERVER")
CONFLUENT_CLUSTER_ID = os.getenv("CONFLUENT_CLUSTER_ID")
CONFLUENT_ENV_ID = os.getenv("CONFLUENT_ENV_ID")
CONFLUENT_TOPIC = os.getenv("CONFLUENT_TOPIC", "trading.nxt.opta.soccer.raw")

# Coventry City v Newcastle United, 2026-10-12 19:00 UTC.
FIXTURE_ID = os.getenv("FIXTURE_ID", "4egq70v53sjh7a2ll4qw3vz10")
# The topic ACL authorizes only this exact group name. The viewer uses assign() and never commits,
# so it does not join the group or move its offsets.
CONSUMER_GROUP = os.getenv("CONSUMER_GROUP", "spatialbetting-disruptive-innovation")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Play-money start balance for every game.
ROULETTE_START_BALANCE = float(os.getenv("ROULETTE_START_BALANCE", "1000"))

# Demo matches: a recorded match played on the real clock, with its 5 Hz ball track.
# Built by tools/build_demo.py from the Elite Soccer Dataset (Bassek et al. 2025, CC BY 4.0).
DEMO_MATCHES = {
    "demo-j03wmx": {"events": "data/demo/j03wmx.json", "ball": "data/demo/j03wmx_ball.json"},
}

KAFKA_CONSUMER_CONFIG = {
    "bootstrap.servers": CONFLUENT_BOOTSTRAP_SERVER,
    "security.protocol": "SASL_SSL",
    "sasl.mechanisms": "PLAIN",
    "sasl.username": CONFLUENT_API_KEY,
    "sasl.password": CONFLUENT_API_SECRET,
    "session.timeout.ms": 45000,
}
