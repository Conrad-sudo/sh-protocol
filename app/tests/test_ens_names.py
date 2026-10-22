"""
Checks that looking up an ENS name can't be used to reach into our own network, and that offchain
names -- Basenames -- still resolve.

Offchain names answer through CCIP-Read (EIP-3668): the name's resolver names a URL and the server
fetches it. Anyone can register a name whose resolver names http://127.0.0.1:8200 or the cloud's
metadata address, so ens_names does that fetching itself, under guards (see its docstring). These
checks try each guard, and the EIP's round trip, against stand-ins:

  - mainnet: a real Web3 over a fake provider, so it is web3 itself that turns a resolver's revert
    into an OffchainLookup -- with web3's own CCIP-Read left ON. The app's shared providers turn it
    off ([6]), but ens_names must not depend on that, so the stand-in leaves it on.
  - the gateway: a real HTTPS server on 127.0.0.1 with a throwaway certificate authority, so TLS, SNI,
    redirects, sizes and slow answers are the real thing. A fake DNS answers for its names.

Nothing leaves this machine. With --live, one more check reads the real thing: jesse.base.eth, through
mainnet (MAINNET_RPC_URL in .env) and Coinbase's gateway. It reads only, and sends no transaction.

Run: python app/tests/test_ens_names.py [--live]
"""
import datetime
import json
import logging
import os
import socket
import ssl
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from checks import check, finish, sign_in   # first: it puts app/ on sys.path for the imports below

# A scratch database for the route checks, set before app modules import and read db.DB_PATH.
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production-0123456789abcdef")
os.environ["COOKIE_SECURE"] = "0"               # the test client speaks http
os.environ["SIWE_DOMAIN"] = "localhost:3000"    # explicit, so a SIWE_DOMAIN in .env cannot move it

import db                                       # noqa: E402
db.DB_PATH = _tmp_db.name
db.init_db()

import requests                                 # noqa: E402
from cryptography import x509                   # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec          # noqa: E402
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID   # noqa: E402
from ens.constants import ENS_MAINNET_ADDR      # noqa: E402
from ens.utils import normal_name_to_hash       # noqa: E402
from eth_abi import abi                         # noqa: E402
from eth_utils import function_signature_to_4byte_selector as selector, keccak  # noqa: E402
from web3 import Web3                           # noqa: E402
from web3.providers.base import BaseProvider    # noqa: E402

import ens_names                                # noqa: E402
from redact import redact                       # noqa: E402

OWNER = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"     # where the stand-in names point
ZERO = "0x" + "00" * 20
RESOLVER = Web3.to_checksum_address("0x" + "e1" * 20)    # the stand-in offchain resolver
PLAIN_RESOLVER = Web3.to_checksum_address("0x" + "e2" * 20)
OTHER_CONTRACT = Web3.to_checksum_address("0x" + "66" * 20)

OFFCHAIN_LOOKUP = bytes.fromhex("556f1830")    # OffchainLookup(address,string[],bytes,bytes4,bytes)
CALLBACK = selector("resolveWithProof(bytes,bytes)")   # what Basenames' resolver names
SUPPORTS_INTERFACE = selector("supportsInterface(bytes4)")
RESOLVE = selector("resolve(bytes,bytes)")
ADDR = selector("addr(bytes32)")
ENSIP10 = bytes.fromhex("9061b923")

REAL_IS_PUBLIC = ens_names._is_public
GATEWAY = None       # the stand-in gateway, started in __main__
TEST_TLS = None      # a client context that trusts the stand-in gateway's certificate authority


def gateway_answer(call_data: bytes) -> bytes:
    """What a genuine gateway answers to `call_data`: stands in for the signature a real one adds."""
    return b"signed:" + keccak(call_data)


# --- a stand-in mainnet ---------------------------------------------------------------------------

class Revert(Exception):
    """A contract reverting with `data`."""

    def __init__(self, data: bytes):
        super().__init__(data)
        self.data = data


class FakeMainnet(BaseProvider):
    """Mainnet as far as ENS goes: the registry, and the resolvers a check deploys. Answers eth_call."""

    def __init__(self):
        super().__init__()
        self.resolver_of: dict[bytes, str] = {}    # namehash -> resolver address
        self.contracts: dict[str, object] = {}     # lowercase address -> callable(calldata) -> bytes
        self.calls: list[tuple[str, bytes]] = []   # (to, calldata) of every eth_call

    def deploy(self, name: str, contract):
        self.resolver_of[bytes(normal_name_to_hash(name))] = contract.address
        self.contracts[contract.address.lower()] = contract

    def is_connected(self, show_traceback: bool = False) -> bool:
        return True

    def make_request(self, method, params):
        if method == "eth_chainId":
            return {"jsonrpc": "2.0", "id": 0, "result": "0x1"}
        if method == "eth_getBlockByNumber":
            # Only web3's own ENS class asks: it checks the chain isn't stale before any lookup.
            block = {"number": "0x10", "hash": "0x" + "ab" * 32, "parentHash": "0x" + "cd" * 32,
                     "timestamp": hex(int(time.time())), "transactions": []}
            return {"jsonrpc": "2.0", "id": 0, "result": block}
        if method != "eth_call":
            raise NotImplementedError(method)
        to, data = params[0]["to"].lower(), bytes.fromhex(params[0]["data"][2:])
        self.calls.append((to, data))
        try:
            if to == ENS_MAINNET_ADDR.lower():
                out = abi.encode(["address"], [self.resolver_of.get(data[4:36], ZERO)])
            else:
                contract = self.contracts.get(to)
                out = contract(data) if contract else b""
        except Revert as revert:
            error = {"code": 3, "message": "execution reverted", "data": "0x" + revert.data.hex()}
            return {"jsonrpc": "2.0", "id": 0, "error": error}
        return {"jsonrpc": "2.0", "id": 0, "result": "0x" + out.hex()}


class OffchainResolver:
    """
    An ENSIP-10 resolver that answers through CCIP-Read, as Basenames' L1 resolver does: resolve()
    reverts with OffchainLookup, and the callback accepts only the gateway's genuine answer before
    returning the address. It asks `rounds` lookups in a row before answering (0: it never stops).

    @param answer  The address it answers, or None for an empty answer.
    @param sender  Who its lookups say raised them; itself unless a check says otherwise.
    """

    def __init__(self, urls, answer=OWNER, rounds=1, sender=None, call_data=None):
        self.address = RESOLVER
        self.urls, self.answer, self.rounds = list(urls), answer, rounds
        self.sender = sender or self.address
        self.call_data = call_data
        self.callbacks: list[tuple[bytes, bytes]] = []    # (response, extraData) each callback got

    def __call__(self, data: bytes) -> bytes:
        called, args = data[:4], data[4:]
        if called == SUPPORTS_INTERFACE:
            return abi.encode(["bool"], [abi.decode(["bytes4"], args)[0] == ENSIP10])
        if called == RESOLVE:
            raise self.lookup(1)
        if called == CALLBACK:
            response, extra = abi.decode(["bytes", "bytes"], args)
            self.callbacks.append((response, extra))
            round_, call_data = abi.decode(["uint256", "bytes"], extra)
            if response != gateway_answer(call_data):
                raise Revert(b"not the gateway's answer")
            if self.rounds == 0 or round_ < self.rounds:
                raise self.lookup(round_ + 1)
            answer = b"" if self.answer is None else abi.encode(["address"], [self.answer])
            return abi.encode(["bytes"], [answer])
        raise Revert(b"")

    def lookup(self, round_: int) -> Revert:
        call_data = self.call_data or f"question {round_}".encode()
        extra = abi.encode(["uint256", "bytes"], [round_, call_data])
        fields = ["address", "string[]", "bytes", "bytes4", "bytes"]
        return Revert(OFFCHAIN_LOOKUP + abi.encode(fields, [self.sender, self.urls, call_data, CALLBACK, extra]))


class PlainResolver:
    """A resolver from before ENSIP-10: addr() for the names it holds, and no CCIP-Read."""

    def __init__(self, records: dict[str, str]):
        self.address = PLAIN_RESOLVER
        self.records = {bytes(normal_name_to_hash(n)): a for n, a in records.items()}

    def __call__(self, data: bytes) -> bytes:
        if data[:4] == SUPPORTS_INTERFACE:
            return abi.encode(["bool"], [False])
        if data[:4] == ADDR:
            return abi.encode(["address"], [self.records.get(data[4:36], ZERO)])
        raise Revert(b"")


# --- stand-ins for DNS and for gateways -----------------------------------------------------------

class FakeDNS:
    """
    Answers ens_names' one lookup per URL from `answers` (host -> addresses, or a function giving
    them), and remembers each host asked. A host it doesn't know doesn't resolve.
    """

    def __init__(self, answers: dict):
        self.answers = answers
        self.asked: list[str] = []

    def __call__(self, host, port, family=0, type=0, proto=0, flags=0):
        self.asked.append(host)
        found = self.answers.get(host)
        if found is None:
            raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known")
        if callable(found):
            found = found()
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, port, 0, 0)) if ":" in ip
                else (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in found]


class FakeGateways:
    """In place of ens_names._fetch: answers every request as a genuine gateway would, and remembers it."""

    def __init__(self):
        self.asked: list[tuple[str, str, bytes]] = []    # (url template, sender, callData)

    def __call__(self, template, sender, call_data, deadline):
        self.asked.append((template, sender, call_data))
        return gateway_answer(call_data)


class Gateway:
    """
    A stand-in CCIP-Read gateway: HTTPS on 127.0.0.1, with a certificate for gateway.test only. The
    first part of the path picks how it answers (/ok, /redirect, /big, /slow, /500 ...); it remembers
    every request and the TLS name (SNI) each connection asked for.
    """

    def __init__(self, cert_file: str, key_file: str):
        gateway = self
        self.requests: list[dict] = []
        self.sni: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                gateway.answer(self, None)

            def do_POST(self):
                gateway.answer(self, self.rfile.read(int(self.headers.get("Content-Length", 0))))

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_file, key_file)
        context.sni_callback = lambda _sock, name, _ctx: self.sni.append(name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.server.handle_error = lambda *_: None   # a client hanging up early is expected here
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, path: str, host: str = "gateway.test") -> str:
        return f"https://{host}:{self.port}{path}"

    def clear(self):
        self.requests.clear()
        self.sni.clear()

    def answer(self, handler: BaseHTTPRequestHandler, body: bytes | None):
        kind = handler.path.split("/")[1]
        self.requests.append({"method": handler.command, "path": handler.path, "host": handler.headers.get("Host"),
                              "type": handler.headers.get("Content-Type"), "body": body})
        if body is not None:
            question = json.loads(body)
            sender, data = question["sender"], question["data"]
        else:
            sender, data = handler.path.split("/")[2:4]
        try:
            if kind == "ok":
                self.send(handler, 200, json.dumps({"data": "0x" + gateway_answer(bytes.fromhex(data[2:])).hex()}))
            elif kind == "redirect":
                handler.send_response(302)
                handler.send_header("Location", self.url(f"/ok/{sender}/{data}"))
                handler.send_header("Content-Length", "0")
                handler.end_headers()
            elif kind == "big":
                self.send(handler, 200, "x" * 70_000)
            elif kind == "big-unsized":
                handler.send_response(200)
                handler.end_headers()                 # no Content-Length: the body runs to the close
                handler.wfile.write(b"x" * 70_000)
            elif kind == "slow":
                handler.send_response(200)
                handler.send_header("Content-Length", "1000")
                handler.end_headers()
                for _ in range(1000):                 # a byte at a time, each well inside a read timeout
                    handler.wfile.write(b" ")
                    handler.wfile.flush()
                    time.sleep(0.2)
            elif kind == "notjson":
                self.send(handler, 200, "<html>hello</html>")
            else:
                self.send(handler, int(kind), json.dumps({"message": "no"}))
        except OSError:
            pass   # the client hung up, as the size and time checks make it

    @staticmethod
    def send(handler: BaseHTTPRequestHandler, status: int, text: str):
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(text)))
        handler.end_headers()
        handler.wfile.write(text.encode())

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def make_certificates(folder: str) -> tuple[str, str, str]:
    """A throwaway certificate authority, and a certificate it signed for gateway.test: the paths of
    (the authority's certificate, the gateway's certificate, the gateway's key)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key, key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ens_names test CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                       content_commitment=False, key_encipherment=False, data_encipherment=False,
                                       key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    leaf = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "gateway.test")]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("gateway.test")]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    paths = [os.path.join(folder, n) for n in ("ca.pem", "gateway.pem", "gateway.key")]
    with open(paths[0], "wb") as f:
        f.write(ca.public_bytes(serialization.Encoding.PEM))
    with open(paths[1], "wb") as f:
        f.write(leaf.public_bytes(serialization.Encoding.PEM))
    with open(paths[2], "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    return paths[0], paths[1], paths[2]


# --- plumbing ---------------------------------------------------------------------------------------

class LogLines(logging.Handler):
    """The messages logged by ens_names (and the contacts route), kept for the checks to read."""

    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())

    def text(self) -> str:
        return "\n".join(self.lines)


LOG = LogLines()


@contextmanager
def patched(target, **values):
    """`target`'s attributes set to `values` for the block, and put back after."""
    saved = {name: getattr(target, name) for name in values}
    for name, value in values.items():
        setattr(target, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(target, name, value)


@contextmanager
def web3_fetches():
    """Every URL web3's OWN CCIP-Read tries to fetch. None gets fetched: each attempt fails here."""
    tried: list[str] = []

    def refuse(_session, url, *args, **kwargs):
        tried.append(url)
        raise requests.ConnectionError("no network in this test")

    with patched(requests.Session, get=refuse, post=refuse):
        yield tried


@contextmanager
def stand_in(*deployments, dns=None, gateways=None, loopback_is_public=False):
    """
    ens_names pointed at a stand-in mainnet with `deployments` ((name, resolver) pairs) registered.

    @param dns                 Answers for the real gateway path (FakeDNS); without it the gateway
                               requests go to `gateways` instead (FakeGateways, made if not given).
    @param loopback_is_public  127.0.0.1 passes the address check, for the checks that need the
                               stand-in gateway on this machine. Every other address is checked as
                               always.
    @yield  (the provider, the FakeGateways or the FakeDNS, the addresses each connection went to)
    """
    w3 = Web3(FakeMainnet())
    for name, contract in deployments:
        w3.provider.deploy(name, contract)
    connections: list[tuple[str, list[str]]] = []
    real_connect = ens_names._connect

    def connect(addresses, host, deadline):
        connections.append((host, [sockaddr[0] for _, sockaddr in addresses]))
        return real_connect(addresses, host, deadline)

    values = {"load_network_config_by_name": lambda _name: (w3, 1), "_connect": connect}
    if dns is None:
        gateways = gateways or FakeGateways()
        values["_fetch"] = gateways
    else:
        values.update(_getaddrinfo=dns, _TLS=TEST_TLS)
        if loopback_is_public:
            values["_is_public"] = lambda address: address == "127.0.0.1" or REAL_IS_PUBLIC(address)
    LOG.lines.clear()
    if GATEWAY:
        GATEWAY.clear()
    with patched(ens_names, **values):
        yield w3.provider, (gateways if dns is None else dns), connections


def resolve_or_error(name: str):
    """ens_names.resolve(name), or the exception it raised."""
    try:
        return ens_names.resolve(name)
    except Exception as e:     # noqa: BLE001 -- the checks look at which one
        return e


# --- the checks -------------------------------------------------------------------------------------

def test_which_addresses_are_public():
    print("\n[1] which addresses count as public")
    refused = {
        "127.0.0.1": "loopback", "10.0.0.5": "private", "172.16.0.1": "private", "192.168.1.1": "private",
        "169.254.169.254": "the cloud metadata address", "100.64.0.1": "carrier NAT",
        "0.0.0.0": "unspecified", "224.0.0.1": "multicast", "255.255.255.255": "broadcast",
        "240.0.0.1": "reserved", "::1": "IPv6 loopback", "::": "IPv6 unspecified",
        "fe80::1": "IPv6 link-local", "fe80::1%lo0": "link-local with a scope",
        "fd00::1": "IPv6 unique-local", "fec0::1": "IPv6 site-local", "ff02::1": "IPv6 multicast",
        "::ffff:127.0.0.1": "loopback inside IPv6", "::ffff:169.254.169.254": "metadata inside IPv6",
        "64:ff9b::a9fe:a9fe": "metadata through NAT64", "2002:a9fe:a9fe::1": "metadata through 6to4",
        "2001:0:4136:e378:8000:63bf:3fff:fdd2": "Teredo", "not-an-address": "not an address",
    }
    for address, what in refused.items():
        check(f"{address} ({what}) is refused", not ens_names._is_public(address))
    for address in ["8.8.8.8", "1.1.1.1", "104.18.32.7", "2606:4700:4700::1111", "2a00:1450:4001:80b::200e"]:
        check(f"{address} is allowed", ens_names._is_public(address))


def test_web3s_own_ccip_read_never_runs():
    print("\n[2] web3's own CCIP-Read is the hole, and ens_names never lets it run")
    metadata = "http://169.254.169.254/latest/meta-data/{sender}/{data}"
    with stand_in(("base.eth", OffchainResolver([metadata])), dns=FakeDNS({})) as (provider, dns, connections):
        w3 = ens_names.load_network_config_by_name("mainnet")[0]
        with web3_fetches() as tried:
            try:
                w3.ens.address("jesse.base.eth")
            except Exception:   # noqa: BLE001 -- every fetch fails here, so web3 gives up
                pass
        check("left to itself, web3 fetches whatever URL a name's resolver gives it",
              any(url.startswith("http://169.254.169.254/latest/meta-data/0x") for url in tried), str(tried))

        with web3_fetches() as tried:
            address = ens_names.resolve("jesse.base.eth")
        check("through ens_names the same name points nowhere", address is None, str(address))
        check("web3 fetched nothing", tried == [], str(tried))
        check("nothing was looked up or connected to", dns.asked == [] and connections == [],
              f"{dns.asked} {connections}")
        check("the shared provider's own CCIP-Read switch is left as it was",
              provider.global_ccip_read_enabled is True)
        check("why goes to the log", "is not a plain https:// URL" in LOG.text(), LOG.text())


def test_the_round_trip():
    print("\n[3] EIP-3668's round trip, as the resolver sees it")
    template = "https://gateway.test/{sender}/{data}"
    resolver = OffchainResolver([template])
    with stand_in(("base.eth", resolver)) as (provider, gateways, _):
        address = ens_names.resolve("jesse.base.eth")
    check("a Basename resolves through its parent's resolver and one lookup", address == OWNER, str(address))
    check("the gateway was asked once, with the lowercase sender and the lookup's callData",
          gateways.asked == [(template, RESOLVER.lower(), b"question 1")], str(gateways.asked))
    extra = abi.encode(["uint256", "bytes"], [1, b"question 1"])
    check("the callback got the gateway's answer and the lookup's extraData, unchanged",
          resolver.callbacks == [(gateway_answer(b"question 1"), extra)], str(resolver.callbacks))
    to, data = provider.calls[-1]
    check("and was called on the contract that raised the lookup", to == RESOLVER.lower() and data[:4] == CALLBACK,
          f"{to} {data[:4].hex()}")

    with stand_in(("base.eth", OffchainResolver([template], sender=OTHER_CONTRACT))) as (_, gateways, _):
        address = ens_names.resolve("jesse.base.eth")
    check("a lookup passed on from another contract is refused (the sender check)",
          address is None and gateways.asked == [], f"{address} {gateways.asked}")
    check("and logged as such", "passed on an offchain lookup" in LOG.text(), LOG.text())

    with stand_in(("base.eth", OffchainResolver([template], rounds=4))) as (_, gateways, _):
        address = ens_names.resolve("jesse.base.eth")
    check("four lookups in a row still resolve", address == OWNER and len(gateways.asked) == 4,
          f"{address} after {len(gateways.asked)}")
    with stand_in(("base.eth", OffchainResolver([template], rounds=0))) as (_, gateways, _):
        address = ens_names.resolve("jesse.base.eth")
    check("a fifth is refused: the name points nowhere after 4 gateway requests",
          address is None and len(gateways.asked) == 4, f"{address} after {len(gateways.asked)}")
    check("and the log says why", "more than 4 offchain lookups" in LOG.text(), LOG.text())

    big = OffchainResolver([template], call_data=b"\x01" * (16 * 1024 + 1))
    with stand_in(("base.eth", big)) as (_, gateways, _):
        address = ens_names.resolve("jesse.base.eth")
    check("a lookup that would send a gateway over 16 KB is refused", address is None and gateways.asked == [],
          f"{address} {len(gateways.asked)}")

    with stand_in(("base.eth", OffchainResolver([template], answer=ZERO))):
        address = ens_names.resolve("jesse.base.eth")
    check("the zero address counts as no address", address is None, str(address))
    with stand_in(("base.eth", OffchainResolver([template], answer=None))):
        address = ens_names.resolve("nobody-yet.base.eth")
    check("so does an empty answer (an unregistered Basename's)", address is None, str(address))

    with stand_in(("sam.eth", PlainResolver({"sam.eth": OWNER}))) as (_, gateways, _):
        found = ens_names.resolve("sam.eth")
        sub = ens_names.resolve("pay.sam.eth")
        nobody = ens_names.resolve("nobody.eth")
    check("a name with an onchain resolver still resolves, with no gateway",
          found == OWNER and gateways.asked == [], f"{found} {gateways.asked}")
    check("an onchain parent's resolver doesn't answer for the names under it", sub is None, str(sub))
    check("a name with no resolver points nowhere", nobody is None, str(nobody))


def test_refused_gateways():
    print("\n[4] gateway URLs and addresses the guards refuse, before any connection")
    public = {"gateway.test": ["93.184.215.14"]}
    cases = [   # (what, the URL a resolver names, what DNS answers, what the log should say)
        ("an http:// URL", "http://gateway.test/{sender}/{data}", public, "is not a plain https:// URL"),
        ("a user:password@ URL", "https://user:pw@gateway.test/{sender}/{data}", public,
         "is not a plain https:// URL"),
        ("a URL with a line break", "https://gateway.test/{sender}\r\nX-Evil: 1/{data}", public,
         "a gateway URL a request can't be made to"),
        ("a host that doesn't resolve", "https://nowhere.test/{sender}/{data}", {}, "nowhere.test does not resolve"),
    ]
    internal = ["127.0.0.1", "10.0.0.5", "192.168.1.10", "169.254.169.254", "100.64.0.1", "0.0.0.0", "::1",
                "fe80::1", "fd00::5", "::ffff:127.0.0.1"]
    cases += [(f"a host at {ip}", "https://evil.test/{sender}/{data}", {"evil.test": [ip]},
               f"resolves to {ip}, which is not a public address") for ip in internal]
    cases.append(("a host with one internal address among public ones", "https://evil.test/{sender}/{data}",
                  {"evil.test": ["93.184.215.14", "2606:2800:220:1::1", "127.0.0.1"]},
                  "resolves to 127.0.0.1, which is not a public address"))
    for label, url, answers, reason in cases:
        with stand_in(("base.eth", OffchainResolver([url])), dns=FakeDNS(answers)) as (_, dns, connections):
            address = resolve_or_error("jesse.base.eth")
        check(f"{label}: the name points nowhere, nothing was connected to, and the log says why",
              address is None and connections == [] and reason in LOG.text(),
              f"{address!r} {connections} {LOG.text()!r}")
    with stand_in(("base.eth", OffchainResolver(["http://gateway.test/{data}"])), dns=FakeDNS({})) as (_, dns, _):
        ens_names.resolve("jesse.base.eth")
    check("an http:// URL isn't even looked up", dns.asked == [], str(dns.asked))

    urls = [f"https://h{i}.test/{{sender}}/{{data}}" for i in range(5)]
    answers = {f"h{i}.test": [f"10.0.0.{i + 1}"] for i in range(5)}
    with stand_in(("base.eth", OffchainResolver(urls)), dns=FakeDNS(answers)) as (_, dns, _):
        address = ens_names.resolve("jesse.base.eth")
    check("only the first 3 of a lookup's URLs are tried", address is None and dns.asked == ["h0.test", "h1.test", "h2.test"],
          str(dns.asked))
    check("the log says why each was refused", all(f"resolves to 10.0.0.{i}" in LOG.text() for i in (1, 2, 3)),
          LOG.text())


def test_the_stand_in_gateway():
    print("\n[5] a gateway over real TLS (127.0.0.1 counts as public for these checks only)")
    gw = GATEWAY
    dns = {"gateway.test": ["127.0.0.1"], "other.test": ["127.0.0.1"]}

    def run(*urls, answers=None):
        with stand_in(("base.eth", OffchainResolver(urls)), dns=FakeDNS(answers or dns),
                      loopback_is_public=True) as (_, fake_dns, connections):
            started = time.monotonic()
            result = resolve_or_error("jesse.base.eth")
            return result, fake_dns, connections, time.monotonic() - started

    address, _, _, _ = run(gw.url("/ok/{sender}/{data}"))
    check("a gateway's GET answer resolves the name", address == OWNER, repr(address))
    sent = gw.requests[0] if gw.requests else {}
    check("asked once, at the path the URL gave, with the lowercase sender and callData",
          len(gw.requests) == 1 and sent["path"] == f"/ok/{RESOLVER.lower()}/0x{b'question 1'.hex()}", str(gw.requests))
    check("TLS named the URL's host (SNI), though the connection went to an address",
          gw.sni == ["gateway.test"], str(gw.sni))
    check("so did the Host header", sent.get("host") == f"gateway.test:{gw.port}", str(sent.get("host")))

    address, _, _, _ = run(gw.url("/ok"))
    sent = gw.requests[0] if gw.requests else {}
    check("a URL without {data} gets a POST, and resolves too", address == OWNER and sent.get("method") == "POST",
          f"{address!r} {sent.get('method')}")
    check("its JSON body carries the callData and the sender",
          sent.get("type") == "application/json" and json.loads(sent.get("body") or b"{}") ==
          {"data": "0x" + b"question 1".hex(), "sender": RESOLVER.lower()}, str(sent))

    rebinding = iter([["127.0.0.1"]] + [["10.66.0.1"]] * 10)
    address, fake_dns, connections, _ = run(gw.url("/ok/{sender}/{data}"),
                                            answers={"gateway.test": lambda: next(rebinding)})
    check("DNS rebinding: the host is looked up once, and the connection goes to the address checked",
          address == OWNER and fake_dns.asked == ["gateway.test"] and connections == [("gateway.test", ["127.0.0.1"])],
          f"{address!r} {fake_dns.asked} {connections}")

    address, _, _, _ = run(gw.url("/redirect/{sender}/{data}"))
    check("a redirect isn't followed: the name points nowhere", address is None, repr(address))
    check("and where it pointed was never asked", [r["path"].split("/")[1] for r in gw.requests] == ["redirect"],
          str(gw.requests))
    check("the log says why", "answered 302 (redirects are not followed)" in LOG.text(), LOG.text())

    address, _, _, _ = run(gw.url("/big/{sender}/{data}"))
    check("an answer that says it is over 64 KB is refused", address is None and "would send 70000 bytes" in LOG.text(),
          f"{address!r} {LOG.text()}")
    address, _, _, _ = run(gw.url("/big-unsized/{sender}/{data}"))
    check("so is one over 64 KB that doesn't say", address is None and "more than 65536 bytes" in LOG.text(),
          f"{address!r} {LOG.text()}")
    address, _, _, _ = run(gw.url("/notjson/{sender}/{data}"))
    check("an answer that isn't EIP-3668's JSON is refused", address is None, repr(address))

    address, _, _, _ = run(gw.url("/404/{sender}/{data}"), gw.url("/ok/{sender}/{data}"))
    check("a gateway's 4xx stops the lookup: the next URL isn't tried",
          address is None and len(gw.requests) == 1, f"{address!r} {len(gw.requests)} requests")
    address, _, _, _ = run(gw.url("/500/{sender}/{data}"), gw.url("/ok/{sender}/{data}"))
    check("a 5xx moves on to the next URL", address == OWNER and len(gw.requests) == 2,
          f"{address!r} {len(gw.requests)} requests")
    result, _, _, _ = run(*[gw.url("/500/{sender}/{data}")] * 5)
    check("only 5xx answers: 'couldn't look up right now', not 'points nowhere'",
          isinstance(result, ens_names.GatewayUnavailable), repr(result))
    check("after 3 of the 5 URLs", len(gw.requests) == 3, f"{len(gw.requests)} requests")

    address, _, _, _ = run(f"https://other.test:{gw.port}/ok/{{sender}}/{{data}}")
    check("a certificate for another name is refused, though the address was allowed",
          address is None and "certificate is not valid" in LOG.text(), f"{address!r} {LOG.text()}")
    check("before any request was sent", gw.requests == [], str(gw.requests))

    with patched(ens_names, ANSWER_TIMEOUT_SECS=1.0):
        result, _, _, took = run(gw.url("/slow/{sender}/{data}"))
    check("a gateway that trickles its answer a byte at a time is cut off at the answer deadline",
          isinstance(result, ens_names.GatewayUnavailable) and took < 2.5, f"{result!r} after {took:.1f} s")
    with patched(ens_names, ANSWER_TIMEOUT_SECS=1.0, NAME_BUDGET_SECS=1.5):
        result, _, _, took = run(*[gw.url("/slow/{sender}/{data}")] * 3)
    check("and one name's gateway requests share one time budget",
          isinstance(result, ens_names.GatewayUnavailable) and took < 2.5 and len(gw.requests) == 2,
          f"{result!r} after {took:.1f} s, {len(gw.requests)} requests")


def test_the_apps_own_providers():
    print("\n[6] the app's own providers never follow an OffchainLookup either")
    import network_config
    from web3.exceptions import OffchainLookup
    from web3.providers.rpc import HTTPProvider

    # Any contract the app calls, a token added by address say, can answer with OffchainLookup.
    chain = FakeMainnet()
    contract = OffchainResolver(["http://169.254.169.254/latest/meta-data/{sender}/{data}"])
    chain.contracts[contract.address.lower()] = contract
    w3 = Web3(network_config._ChainIdOnceProvider("http://127.0.0.1:9"))
    check("the shared providers have web3's CCIP-Read switched off",
          w3.provider.global_ccip_read_enabled is False)

    call = {"to": contract.address, "data": "0x" + (RESOLVE + abi.encode(["bytes", "bytes"], [b"", b""])).hex()}
    over_the_wire = lambda _provider, method, params: chain.make_request(method, params)  # noqa: E731
    with patched(HTTPProvider, make_request=over_the_wire), web3_fetches() as tried:
        try:
            w3.eth.call(call)
            outcome = "answered"
        except OffchainLookup:
            outcome = "OffchainLookup"
        except Exception as error:   # noqa: BLE001 -- named in the check's message
            outcome = repr(error)
    check("the call fails with OffchainLookup instead", outcome == "OffchainLookup", outcome)
    check("web3 fetched nothing", tried == [], str(tried))


def test_the_route():
    print("\n[7] the contact form's lookup: a refusal says no more than 'points nowhere'")
    from contextlib import asynccontextmanager

    from fastapi.testclient import TestClient

    import api

    @asynccontextmanager
    async def _no_lifespan(_app):
        yield

    api.app.router.lifespan_context = _no_lifespan
    api.limiter.enabled = False
    client = TestClient(api.app)
    _, headers, _ = sign_in(client)

    metadata_host = "metadata.evil.test"
    with stand_in(("base.eth", OffchainResolver([f"https://{metadata_host}/{{sender}}/{{data}}"])),
                  dns=FakeDNS({metadata_host: ["169.254.169.254"]})) as (_, _, connections):
        r = client.get("/api/contacts/resolve", headers=headers, params={"name": "jesse.base.eth"})
    check("a name whose gateway is refused -> 404", r.status_code == 404, f"{r.status_code} {r.text[:160]}")
    check("with the route's usual words", r.json().get("detail") == "jesse.base.eth doesn't point to an address",
          r.text[:160])
    check("and nothing about the gateway or its address", "169.254" not in r.text and "evil" not in r.text, r.text)
    check("no connection was made", connections == [], str(connections))
    check("the log has the reason", "169.254.169.254, which is not a public address" in LOG.text(), LOG.text())

    with stand_in(("base.eth", OffchainResolver([GATEWAY.url("/500/{sender}/{data}")])),
                  dns=FakeDNS({"gateway.test": ["127.0.0.1"]}), loopback_is_public=True):
        r = client.get("/api/contacts/resolve", headers=headers, params={"name": "jesse.base.eth"})
    check("a gateway that is down -> 503, 'couldn't look up right now'",
          r.status_code == 503 and r.json().get("detail") == "Couldn't look up jesse.base.eth right now",
          f"{r.status_code} {r.text[:160]}")
    check("without the gateway's name either", "gateway.test" not in r.text, r.text)

    with stand_in(("base.eth", OffchainResolver(["https://gateway.test/{sender}/{data}"]))):
        r = client.get("/api/contacts/resolve", headers=headers, params={"name": "Jesse.Base.ETH"})
    check("a Basename still answers its address through the route",
          r.status_code == 200 and r.json() == {"name": "jesse.base.eth", "address": OWNER}, r.text[:160])


def test_live():
    print("\n[live] a real Basename, through mainnet and its real gateway (reads only)")
    url = os.getenv("MAINNET_RPC_URL")
    if not url:
        print("  SKIP  MAINNET_RPC_URL is not set")
        return
    w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 20}))
    started = time.monotonic()
    with patched(ens_names, load_network_config_by_name=lambda _name: (w3, 1)):
        try:
            address = ens_names.resolve("jesse.base.eth")
        except Exception as e:     # noqa: BLE001 -- reported, with the RPC's key masked
            address = f"{type(e).__name__}: {redact(str(e))}"
    check("jesse.base.eth resolves to an address", isinstance(address, str) and Web3.is_checksum_address(address),
          str(address))
    print(f"        {address}, in {time.monotonic() - started:.1f} s")


if __name__ == "__main__":
    for logger_name in ("ens_names", "routes.contacts"):
        logger = logging.getLogger(logger_name)
        logger.addHandler(LOG)
        logger.propagate = False    # the refusals these checks provoke are read here, not printed
    certificates = tempfile.TemporaryDirectory()
    ca_file, cert_file, key_file = make_certificates(certificates.name)
    TEST_TLS = ens_names._tls_context(ca_file)
    GATEWAY = Gateway(cert_file, key_file)
    try:
        test_which_addresses_are_public()
        test_web3s_own_ccip_read_never_runs()
        test_the_round_trip()
        test_refused_gateways()
        test_the_stand_in_gateway()
        test_the_apps_own_providers()
        test_the_route()
        if "--live" in sys.argv[1:]:
            test_live()
    finally:
        GATEWAY.close()
        certificates.cleanup()
        os.unlink(_tmp_db.name)

    finish("All ENS lookup checks passed.")
