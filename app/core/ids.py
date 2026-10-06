import os
import time
import uuid


def uuid7() -> uuid.UUID:
    """RFC 9562 UUIDv7: time-ordered, so primary-key indexes stay append-mostly.

    Devices generate the same kind of id offline, so a row keeps one identity
    from the till to the server.
    """
    ms = int(time.time() * 1000)
    rand = int.from_bytes(os.urandom(10), "big")
    rand_a = (rand >> 62) & 0xFFF
    rand_b = rand & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= rand_a << 64
    value |= 0b10 << 62
    value |= rand_b
    return uuid.UUID(int=value)
