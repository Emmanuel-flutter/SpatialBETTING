import json
import signal

from confluent_kafka import Consumer, KafkaError, KafkaException

from config import CONFLUENT_TOPIC, KAFKA_CONSUMER_CONFIG

running = True


def shutdown(sig, frame):
    global running
    running = False


signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)


def create_consumer(group_id="spatialbetting-disruptive-innovation", offset_reset="latest"):
    conf = {
        **KAFKA_CONSUMER_CONFIG,
        "group.id": group_id,
        "auto.offset.reset": offset_reset,
        "enable.auto.commit": True,
    }
    return Consumer(conf)


def consume(topic=None, group_id=None, on_message=None):
    topic = topic or CONFLUENT_TOPIC
    consumer = create_consumer(group_id=group_id or "spatialbetting-disruptive-innovation")
    consumer.subscribe([topic])
    print(f"Subscribed to {topic}. Waiting for messages...")

    try:
        while running:
            msg = consumer.poll(timeout=1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                raise KafkaException(msg.error())

            try:
                value = json.loads(msg.value().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                value = msg.value()

            if on_message:
                on_message(value, msg)
            else:
                _default_handler(value, msg)
    finally:
        consumer.close()
        print("Consumer closed.")


def _default_handler(value, msg):
    if isinstance(value, dict):
        print(json.dumps(value, indent=2)[:2000])
    else:
        print(f"[raw] {value[:500]}")
    print(f"--- partition={msg.partition()} offset={msg.offset()}\n")
