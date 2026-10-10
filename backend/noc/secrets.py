import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import settings


def instance_key() -> bytes:
    directory = Path(settings.secret_directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "instance.key"
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        key = path.read_bytes()
    else:
        key = AESGCM.generate_key(bit_length=256)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(key)
    if len(key) != 32:
        raise ValueError("Invalid instance encryption key")
    return key


def encrypt_config(value: dict) -> str:
    nonce = os.urandom(12)
    payload = AESGCM(instance_key()).encrypt(nonce, json.dumps(value).encode(), b"unifi-noc-connection-v1")
    return base64.b64encode(nonce + payload).decode()


def decrypt_config(value: str) -> dict:
    payload = base64.b64decode(value, validate=True)
    return json.loads(AESGCM(instance_key()).decrypt(payload[:12], payload[12:], b"unifi-noc-connection-v1"))