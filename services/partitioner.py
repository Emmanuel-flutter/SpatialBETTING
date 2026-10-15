def murmur2(data: bytes) -> int:
    """Kafka's Java murmur2 hash, the default producer partitioner for keyed messages."""
    m, r, length = 0x5BD1E995, 24, len(data)
    h = (0x9747B28C ^ length) & 0xFFFFFFFF
    for i in range(0, length - length % 4, 4):
        k = int.from_bytes(data[i:i + 4], "little")
        k = (k * m) & 0xFFFFFFFF
        k ^= k >> r
        k = (k * m) & 0xFFFFFFFF
        h = (h * m) & 0xFFFFFFFF
        h ^= k
    tail, rem = data[length - length % 4:], length % 4
    if rem == 3:
        h ^= tail[2] << 16
    if rem >= 2:
        h ^= tail[1] << 8
    if rem >= 1:
        h ^= tail[0]
        h = (h * m) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * m) & 0xFFFFFFFF
    h ^= h >> 15
    return h


def partition_for(key: str, partitions: int) -> int:
    return (murmur2(key.encode()) & 0x7FFFFFFF) % partitions
