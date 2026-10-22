"""
ENS names -> addresses, so the owner can add a contact by its name (sam.eth) instead of its 0x address.

Used only when the OWNER adds a contact in the web app. The address found here goes back through
the usual prepare -> sign -> save (routes/contacts.py), so what the owner signs and what is stored is
the address itself, and nothing reads the name again. A name that later changes hands, expires or is
pointed somewhere else therefore never redirects a payment to a saved contact: the owner approved an
address, not a name.

The agent never resolves names. To it, "sam.eth" is only a contact name, and one that isn't saved is
refused like any other (tools.common._resolve_contact) -- otherwise anyone able to type into the chat
could register a name and have it paid.

Offchain names -- Basenames (jesse.base.eth) and the other names an L2 or an app hands out -- answer
through CCIP-Read (EIP-3668): the name's resolver reverts with OffchainLookup(urls, ...), the client
fetches one of those URLs and hands the answer back to the resolver, which checks it. Anyone can
register a name and give it a resolver that names ANY url, and the fetch is made by this server.
web3 does CCIP-Read by itself unless told not to, so through it a name typed into the contact form
could make the API request a port on localhost, 169.254.169.254 (cloud metadata) or any
other host on our network, and follow a redirect to one as well. Switching CCIP-Read off would lose
every Basename. So web3's stays off (the app's shared providers turn it off for every call, see
network_config._ChainIdOnceProvider, and each call here passes ccip_read_enabled=False as well, so
this module never depends on that), and the lookups are made here instead, under these guards:

  - https:// only, and the certificate must be valid for the URL's host, so a request is only ever
    sent to a server holding a publicly trusted certificate for that name.
  - the host is looked up once, and refused if ANY address it resolves to is not public: private,
    loopback, link-local (169.254.169.254 is one), multicast, reserved, unspecified, carrier NAT, or
    an IPv6 address with such an IPv4 one inside it. The connection then goes to an address that was
    checked, never to the name again: a DNS server that says "public" to the check and "127.0.0.1"
    a moment later (DNS rebinding) is never asked the second time. TLS still sends the name (SNI)
    and checks the certificate against it, so connecting by address loses nothing.
  - no redirects are followed; 3 s to connect, 5 s for the whole answer however slowly it trickles
    in, 10 s for all of one name's gateway requests together; an answer over 64 KB is refused.
  - at most 4 lookups per call (EIP-3668's number) and 3 URLs per lookup. A lookup must come from the
    contract that was called (the EIP's sender check: a contract may not pass on a lookup raised by
    another one), and the gateway's answer goes back to that contract's callback with the lookup's
    extraData exactly as given -- the resolver, not this code, judges whether the answer is genuine.

A refused gateway means the name doesn't resolve: resolve() returns None, the route says the name
points nowhere, and the reason goes to the log only. A gateway that is merely down or slow raises
GatewayUnavailable instead, which the route reports as "couldn't look up right now".
"""
import http.client
import ipaddress
import json
import logging
import re
import socket
import ssl
import time
from urllib.parse import urlsplit

import certifi
from ens.constants import ENS_EXTENDED_RESOLVER_INTERFACE_ID, ENS_MAINNET_ADDR
from ens.exceptions import InvalidName
from ens.utils import dns_encode_name, normal_name_to_hash, normalize_name
from eth_abi import abi
from eth_utils import function_signature_to_4byte_selector, to_checksum_address
from web3.exceptions import OffchainLookup

from network_config import load_network_config_by_name

logger = logging.getLogger(__name__)

# ENS lives on Ethereum mainnet whatever chain the wallet is on (Basenames and other L2 names are
# answered from mainnet too, through their offchain gateways). Read from the live chain even in
# fork mode: a fork's ENS records are as old as the fork, and a contact saved there should point
# where the name really points.
ENS_NETWORK = "mainnet"

# The guards described above. A Basename's gateway answers in under a second with ~600 bytes.
MAX_LOOKUPS = 4                    # OffchainLookups one call may make (EIP-3668's number)
MAX_URLS_PER_LOOKUP = 3            # gateway URLs tried for one lookup, in the order given
CONNECT_TIMEOUT_SECS = 3.0         # the TCP connect, and then the TLS handshake
ANSWER_TIMEOUT_SECS = 5.0          # the whole answer, however slowly it arrives
NAME_BUDGET_SECS = 10.0            # all of one name's gateway requests together
MAX_ANSWER_BYTES = 64 * 1024       # the whole answer: status line, headers and body
MAX_CALL_DATA_BYTES = 16 * 1024    # what a resolver may have us send to a gateway
MAX_URL_TEMPLATE_CHARS = 2048      # a real one is under 100

_RESOLVER = function_signature_to_4byte_selector("resolver(bytes32)")
_SUPPORTS_INTERFACE = function_signature_to_4byte_selector("supportsInterface(bytes4)")
_RESOLVE = function_signature_to_4byte_selector("resolve(bytes,bytes)")   # ENSIP-10
_ADDR = function_signature_to_4byte_selector("addr(bytes32)")
_ENSIP10 = bytes.fromhex(ENS_EXTENDED_RESOLVER_INTERFACE_ID[2:])
_HEX = re.compile(r"0x(?:[0-9a-fA-F]{2})*")

# The one DNS lookup made for a gateway URL. A name, not a direct call, so the tests can answer it.
_getaddrinfo = socket.getaddrinfo


class GatewayUnavailable(Exception):
    """No gateway the name's resolver named answered in time: worth trying again later."""


class _Refused(Exception):
    """A lookup the guards won't make, or an answer they won't use: the name points nowhere."""

    def __init__(self, reason: str, stop: bool = False):
        super().__init__(reason)
        # The gateway itself said no (a 4xx): EIP-3668 says stop there, not try the next URL.
        self.stop = stop


def normalize(name: str) -> str:
    """
    `name` as ENS normalises it (ENSIP-15: lowercased, with invisible and right-to-left characters
    refused), the form the owner sees, the lookup uses and the contact is saved under.

    @raises InvalidName  If it isn't a valid ENS name, or has no "." (a bare label names nothing).
    """
    normalized = normalize_name(name.strip())
    if "." not in normalized:
        raise InvalidName(f"'{name}' has no ending such as .eth")
    return normalized


def resolve(name: str) -> str | None:
    """
    The Ethereum address a normalised ENS name points to now, or None if it points nowhere.

    The same steps as web3's ENS.address() -- the registry, the nearest resolver, then ENSIP-10's
    resolve() or a plain addr() -- with every call made through _call(), so CCIP-Read happens under
    the guards above. Unlike web3, the zero address and an empty answer (what an unregistered
    Basename gets back) both count as no address.

    @param name  A name already passed through normalize().
    @return      The checksummed address, or None for an unregistered name, one with no address, or
                 one whose gateway the guards refused (logged, never shown).
    @raises GatewayUnavailable  The name's gateway could not be reached in time.
    """
    w3, _ = load_network_config_by_name(ENS_NETWORK)
    try:
        return _address(w3, name, deadline=time.monotonic() + NAME_BUDGET_SECS)
    except _Refused as refusal:
        logger.warning("ENS name %s treated as pointing nowhere: %s", name, refusal)
        return None


def _address(w3, name: str, deadline: float) -> str | None:
    """resolve()'s lookup, before refusals are turned into None."""
    resolver, found_on = _find_resolver(w3, name, deadline)
    if resolver is None:
        return None
    node = normal_name_to_hash(name)
    query = _SUPPORTS_INTERFACE + abi.encode(["bytes4"], [_ENSIP10])
    (extended,) = abi.decode(["bool"], _call(w3, resolver, query, deadline))
    if extended:
        query = _RESOLVE + abi.encode(["bytes", "bytes"], [dns_encode_name(name), _ADDR + node])
        (answer,) = abi.decode(["bytes"], _call(w3, resolver, query, deadline))
        if not answer:
            return None
        (address,) = abi.decode(["address"], answer)
    elif found_on == name:
        (address,) = abi.decode(["address"], _call(w3, resolver, _ADDR + node, deadline))
    else:
        # A parent's resolver from before ENSIP-10 answers only for its own name.
        return None
    return to_checksum_address(address) if int(address, 16) else None


def _find_resolver(w3, name: str, deadline: float) -> tuple[str | None, str]:
    """
    The resolver set on `name`, or else on its nearest parent that has one (ENSIP-10: a parent's
    resolver may answer for the names below it), and the name it is set on.
    """
    current = name
    while current:
        query = _RESOLVER + normal_name_to_hash(current)
        (resolver,) = abi.decode(["address"], _call(w3, ENS_MAINNET_ADDR, query, deadline))
        if int(resolver, 16):
            return to_checksum_address(resolver), current
        current = current.partition(".")[2]
    return None, ""


def _call(w3, to: str, data: bytes, deadline: float) -> bytes:
    """
    eth_call `data` on the contract `to`, with EIP-3668's client steps done here instead of by web3.

    @raises _Refused            A lookup the guards refuse, or more than MAX_LOOKUPS of them.
    @raises GatewayUnavailable  No gateway for a lookup answered in time.
    """
    lookups = 0
    while True:
        try:
            # Off for this call only: the provider's own switch is shared by the whole app.
            return bytes(w3.eth.call({"to": to, "data": data}, ccip_read_enabled=False))
        except OffchainLookup as revert:
            lookup = revert.payload
        lookups += 1
        if lookups > MAX_LOOKUPS:
            raise _Refused(f"{to} asked for more than {MAX_LOOKUPS} offchain lookups")
        sender = lookup["sender"].lower()
        if sender != to.lower():
            # A contract passing on a lookup raised by another contract it called.
            raise _Refused(f"{to} passed on an offchain lookup from {sender}")
        answer = _ask_gateways(lookup["urls"], sender, lookup["callData"], deadline)
        # Then the callback, on the same contract, with the answer and extraData as they came.
        data = lookup["callbackFunction"] + abi.encode(["bytes", "bytes"], [answer, lookup["extraData"]])


def _ask_gateways(urls, sender: str, call_data: bytes, deadline: float) -> bytes:
    """
    The first usable answer from a lookup's gateway URLs, tried in the order given, at most
    MAX_URLS_PER_LOOKUP of them.

    @raises _Refused            Every URL tried was refused, or a gateway answered with a 4xx.
    @raises GatewayUnavailable  None answered, and at least one could not be reached in time.
    """
    if len(call_data) > MAX_CALL_DATA_BYTES:
        raise _Refused(f"an offchain lookup that would send {len(call_data)} bytes")
    problems, unavailable = [], False
    for template in urls[:MAX_URLS_PER_LOOKUP]:
        try:
            return _fetch(template, sender, call_data, deadline)
        except _Refused as e:
            if e.stop:
                raise
            problems.append(str(e))
        except GatewayUnavailable as e:
            unavailable = True
            problems.append(str(e))
    reason = "; ".join(problems) or "an offchain lookup naming no gateway"
    raise GatewayUnavailable(reason) if unavailable else _Refused(reason)


def _fetch(template: str, sender: str, call_data: bytes, deadline: float) -> bytes:
    """
    One gateway request, as EIP-3668 has it: GET when the URL has {data} in it, else a POST of
    {"data", "sender"} as JSON. Returns the "data" of the answer.

    @raises _Refused            The URL, its addresses or the answer is not one the guards accept.
    @raises GatewayUnavailable  The gateway could not be reached, or did not answer in time.
    """
    if time.monotonic() >= deadline:
        raise GatewayUnavailable("out of time for gateway requests")
    data = "0x" + call_data.hex()
    url = template.replace("{sender}", sender).replace("{data}", data)
    host, port, target = _check_url(template, url)
    addresses = _public_addresses(host, port)
    headers = {"Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": "ens-ccip-read"}
    if "{data}" in template:
        method, body = "GET", None
    else:
        method, body = "POST", json.dumps({"data": data, "sender": sender}).encode()
        headers["Content-Type"] = "application/json"
    connection = http.client.HTTPSConnection(host, port, context=_TLS)
    try:
        # Set here, so http.client never opens a connection of its own -- one that would look the
        # host up again.
        connection.sock = _connect(addresses, host, deadline)
        connection.request(method, target, body=body, headers=headers)
        response = connection.getresponse()
        if 400 <= response.status < 500:
            raise _Refused(f"{host} answered {response.status}", stop=True)
        if 500 <= response.status < 600:
            raise GatewayUnavailable(f"{host} answered {response.status}")
        if not 200 <= response.status < 300:
            raise _Refused(f"{host} answered {response.status} (redirects are not followed)")
        length = response.getheader("Content-Length", "")
        if length.isdigit() and int(length) > MAX_ANSWER_BYTES:
            raise _Refused(f"{host} would send {length} bytes")
        # In pieces: http.client sets aside a whole Content-Length at once when asked for all of it.
        raw = b"".join(iter(lambda: response.read(16 * 1024), b""))
    except ssl.SSLCertVerificationError:
        raise _Refused(f"{host}'s certificate is not valid for it") from None
    except (OSError, http.client.HTTPException) as e:
        raise GatewayUnavailable(f"{host}: {str(e) or type(e).__name__}") from None
    except ValueError:
        # http.client's word for an answer it can't read, such as a chunk size that isn't a number.
        raise _Refused(f"{host} sent an answer that isn't HTTP") from None
    finally:
        connection.close()
    return _answer_data(host, raw)


def _check_url(template: str, url: str) -> tuple[str, int, str]:
    """
    The host, port and request target of a gateway URL.

    @raises _Refused  It isn't a plain https:// URL (no user:password@ either), or is unreadable.
    """
    if len(template) > MAX_URL_TEMPLATE_CHARS or not url.isascii() or not url.isprintable() or " " in url:
        raise _Refused(f"a gateway URL a request can't be made to: {url[:100]!r}")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise _Refused(f"an unreadable gateway URL: {url[:100]!r}") from None
    if parts.scheme != "https" or not parts.hostname or "@" in parts.netloc:
        raise _Refused(f"{url[:100]!r} is not a plain https:// URL")
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return parts.hostname, 443 if port is None else port, target


def _public_addresses(host: str, port: int) -> list[tuple[int, tuple]]:
    """
    Every address `host` resolves to, as (family, socket address), all of them public. The caller
    connects only to these: the host is never looked up a second time.

    A failed lookup is refused rather than retried later, so an internal name that exists answers
    the same as one that doesn't.

    @raises _Refused  It resolves to nothing, or to any address that is not public.
    """
    try:
        found = [(family, sockaddr) for family, _, _, _, sockaddr in _getaddrinfo(host, port, type=socket.SOCK_STREAM)]
    except (OSError, UnicodeError):
        raise _Refused(f"{host} does not resolve") from None
    if not found:
        raise _Refused(f"{host} does not resolve")
    for family, sockaddr in found:
        if family not in (socket.AF_INET, socket.AF_INET6) or not _is_public(sockaddr[0]):
            raise _Refused(f"{host} resolves to {sockaddr[0]}, which is not a public address")
    return found


def _is_public(address: str) -> bool:
    """
    True only for an address on the public internet.

    is_global alone is not enough on this Python (3.12.3): it counts multicast and IPv6 site-local
    (fec0::/10) as global, and an IPv6 address can carry an IPv4 one inside it (6to4, Teredo,
    ::ffff:a.b.c.d), which must pass as well. Carrier NAT (100.64.0.0/10) is caught by is_global.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version == 6:
        inner = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if ip.is_site_local or (inner is not None and not _is_public(str(inner))):
            return False
    return ip.is_global and not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
    )


def _connect(addresses: list[tuple[int, tuple]], host: str, deadline: float) -> ssl.SSLSocket:
    """
    A TLS connection to the first of `addresses` that answers -- addresses already checked -- with
    `host` as the TLS name: sent as SNI, and the certificate checked against it. Its reads then end
    ANSWER_TIMEOUT_SECS later, or at `deadline` if that comes first.
    """
    error: OSError = TimeoutError("ran out of time to connect")
    for family, sockaddr in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sock = None
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.settimeout(min(CONNECT_TIMEOUT_SECS, remaining))   # the TLS handshake gets the same
            sock.connect(sockaddr)
        except OSError as e:   # refused, timed out, or no IPv6 on this machine: try the next one
            if sock is not None:
                sock.close()
            error = e
            continue
        try:
            tls = _TLS.wrap_socket(sock, server_hostname=host)
        except BaseException:
            sock.close()
            raise
        tls.deadline = min(deadline, time.monotonic() + ANSWER_TIMEOUT_SECS)
        return tls
    raise error


class _DeadlineSSLSocket(ssl.SSLSocket):
    """
    A TLS socket whose reads all end by one deadline, and stop at MAX_ANSWER_BYTES in all. A timeout
    per read alone would let a gateway that sends a byte every few seconds hold the request -- and
    the server thread running it -- for as long as it liked.
    """

    deadline = 0.0     # set by _connect once the handshake is done; until then every read refuses
    received = 0

    def recv_into(self, buffer, nbytes=None, flags=0):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("did not answer in time")
        self.settimeout(remaining)
        count = super().recv_into(buffer, nbytes, flags)
        self.received += count
        if self.received > MAX_ANSWER_BYTES:
            raise _Refused(f"{self.server_hostname} answered with more than {MAX_ANSWER_BYTES} bytes")
        return count


def _tls_context(cafile: str) -> ssl.SSLContext:
    """Certificates checked against `cafile`, the host name included; sockets are _DeadlineSSLSocket."""
    context = ssl.create_default_context(cafile=cafile)
    context.set_alpn_protocols(["http/1.1"])
    context.sslsocket_class = _DeadlineSSLSocket
    return context


# certifi's roots, the ones requests (and so web3's own CCIP-Read) trusted.
_TLS = _tls_context(certifi.where())


def _answer_data(host: str, raw: bytes) -> bytes:
    """
    The "data" of a gateway's JSON answer, as bytes.

    @raises _Refused  The answer isn't JSON with a 0x-hex "data" in it.
    """
    try:
        data = json.loads(raw)["data"]
    except (ValueError, KeyError, TypeError, RecursionError):
        data = None
    if not isinstance(data, str) or not _HEX.fullmatch(data):
        raise _Refused(f'{host} answered without a 0x-hex "data"')
    return bytes.fromhex(data[2:])
