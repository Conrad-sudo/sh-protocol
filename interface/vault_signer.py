import os
import base64
import hvac
from dotenv import load_dotenv

load_dotenv()

_KEY_NAME = "session-keys"


def _client() -> hvac.Client:
    client = hvac.Client(url=os.getenv("VAULT_ADDR"))
    client.auth.approle.login(
        role_id=os.getenv("VAULT_ROLE_ID"),
        secret_id=os.getenv("VAULT_SECRET_ID"),
    )
    return client


def encrypt_key(raw_key: bytes) -> str:
    b64 = base64.b64encode(raw_key).decode()
    result = _client().secrets.transit.encrypt_data(name=_KEY_NAME, plaintext=b64)
    return result["data"]["ciphertext"]


def decrypt_key(ciphertext: str) -> bytes:
    result = _client().secrets.transit.decrypt_data(
        name=_KEY_NAME, ciphertext=ciphertext
    )
    return base64.b64decode(result["data"]["plaintext"])
