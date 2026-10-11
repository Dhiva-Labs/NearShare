"""The TLS identity this machine presents to AirDrop peers.

AirDrop runs over TLS, and in "Everyone" mode neither side verifies who
the other is: senders and receivers present self-signed certificates as
a matter of course, and iOS 26 accepts an unsigned peer in both
directions. What Apple devices *can* verify is an Apple-issued identity
(a certificate plus a signed validation record bound to an Apple ID),
which is what "Contacts Only" requires. That cannot be produced off
Apple hardware, so NearShare only ever speaks Everyone mode, and this
module just keeps one self-signed RSA pair, created on first use.

Stored at $XDG_DATA_HOME/nearshare/airdrop/{key.pem,cert.pem}, 0600.
"""

from __future__ import annotations

import datetime
import logging
import os
import ssl
import uuid
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("nearshare.airdrop.identity")


def identity_dir() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME") or str(
        Path.home() / ".local" / "share")
    return Path(data_home) / "nearshare" / "airdrop"


@dataclass
class Identity:
    cert_path: Path
    key_path: Path

    def server_context(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(self.cert_path, self.key_path)
        # Requesting a client certificate buys nothing in Everyone mode
        # (nothing consults it) and can only turn a sender whose chain
        # does not verify into a handshake failure.
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def client_context(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.load_cert_chain(self.cert_path, self.key_path)
        return ctx


def load_or_create(directory: Path | None = None) -> Identity:
    directory = directory or identity_dir()
    cert_path = directory / "cert.pem"
    key_path = directory / "key.pem"
    if cert_path.is_file() and key_path.is_file():
        return Identity(cert_path, key_path)

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    # The common name is a random UUID, as Apple's own self-signed
    # AirDrop certificates carry, so nothing here names the machine.
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                         str(uuid.uuid4()).upper())])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .sign(key, hashes.SHA256()))

    def write_private(path: Path, data: bytes) -> None:
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)

    write_private(key_path, key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    write_private(cert_path, cert.public_bytes(serialization.Encoding.PEM))
    log.info("created AirDrop TLS identity in %s", directory)
    return Identity(cert_path, key_path)
