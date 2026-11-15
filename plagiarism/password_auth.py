"""Bounded password hashing shared by administrator and local user adapters."""

from hashlib import scrypt
from threading import BoundedSemaphore

from plagiarism.access import AccessError

SCRYPT_N = 32768
SCRYPT_R = 8
SCRYPT_P = 1
HASH_SLOTS = BoundedSemaphore(2)


def password_digest(password, salt):
    if not HASH_SLOTS.acquire(blocking=False):
        error = AccessError("Слишком много запросов входа. Повторите позже.", "auth_busy", 429)
        error.retry_after = 5
        raise error
    try:
        return scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=SCRYPT_N,
                      r=SCRYPT_R, p=SCRYPT_P, dklen=32, maxmem=64 * 1024 * 1024).hex()
    finally:
        HASH_SLOTS.release()
