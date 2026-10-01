"""
Shared plumbing for the test scripts in this folder.

Importing it puts app/ on sys.path, so a test can `import db`, `import api` and the rest exactly as
the app's own modules do. `python app/tests/test_x.py` only puts app/tests/ there, so every test
must import this BEFORE any app module.
"""
import sys
from pathlib import Path

APP_DIR = str(Path(__file__).resolve().parent.parent)
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

failures: list[str] = []


def check(label: str, condition: bool, detail: str = ""):
    """Prints one PASS/FAIL line. A failure is recorded, not raised, so one run reports every broken check."""
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{': ' + detail if detail else ''}")
        failures.append(label)


def finish(success_message: str):
    """Prints the run's summary and exits non-zero if any check failed."""
    print()
    if failures:
        print(f"FAILED ({len(failures)}): {failures}")
        sys.exit(1)
    print(success_message)


def sign_in(client, account=None, chain_id: int = 11155111):
    """
    Signs in the way the web app does -- a SIWE message, the only way in -- as `account`, or as a
    fresh address. The first sign-in for an address creates its account.

    @param client  A TestClient on the API.
    @return        (the token response, its auth headers, the eth_account account that signed).
    """
    import auth
    from eth_account import Account
    from eth_account.messages import encode_defunct

    account = account or Account.create()
    nonce = client.get("/api/auth/siwe/nonce").json()["nonce"]
    # Whatever this server accepts, so a SIWE_DOMAIN in .env cannot break the tests.
    domain = sorted(auth.SIWE_DOMAINS)[0]
    message = auth.build_siwe_message(domain, account.address, nonce, chain_id)
    signature = Account.sign_message(encode_defunct(text=message), account.key).signature.hex()
    r = client.post("/api/auth/siwe/login", json={"message": message, "signature": signature, "nonce": nonce})
    if r.status_code != 200:
        raise AssertionError(f"SIWE sign-in failed: {r.status_code} {r.text[:200]}")
    body = r.json()
    return body, {"Authorization": f"Bearer {body['access_token']}"}, account


def add_contact(client, headers: dict, account, name: str, address: str):
    """
    Saves a contact the way the web app does: POST /api/contacts/prepare, sign the typed data it
    returns with `account` (the owner, unless a test means otherwise), then POST /api/contacts.

    @return  The response of the step that answered last: prepare's if it refused, else the save's.
    """
    from eth_account import Account

    r = client.post("/api/contacts/prepare", headers=headers, json={"name": name, "address": address})
    if r.status_code != 200:
        return r
    typed = r.json()
    signed = Account.sign_typed_data(account.key, typed["domain"], typed["types"], typed["message"])
    return client.post("/api/contacts", headers=headers, json={**typed["message"], "signature": signed.signature.hex()})
