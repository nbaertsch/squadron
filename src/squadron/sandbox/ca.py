"""Ephemeral CA certificate generation for sandbox TLS interception.

Generates a self-signed CA at startup.  The CA cert is injected into agent
namespaces via SSL_CERT_FILE / NODE_EXTRA_CA_CERTS so that the MitM proxy
can terminate and re-originate HTTPS connections transparently.

The CA key never enters the agent namespace — it stays host-side, used
only by the InferenceProxy to sign per-upstream leaf certificates on the fly.
"""

from __future__ import annotations

import datetime
import logging
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

logger = logging.getLogger(__name__)

# File names within the CA directory.
_CA_KEY_FILE = "ca.key"
_CA_CERT_FILE = "ca.crt"


class SandboxCA:
    """Manages an ephemeral CA for sandbox TLS interception.

    Usage::

        ca = SandboxCA("/tmp/squadron-ca", validity_days=1)
        ca.ensure_ca()
        # ca.cert_path  -> Path to PEM cert (inject into agent env)
        # ca.key_path   -> Path to PEM key (host-side only, for proxy)
        # ca.sign_leaf(hostname) -> (cert_pem, key_pem) for upstream proxy
    """

    def __init__(self, ca_dir: str, validity_days: int = 1) -> None:
        self._ca_dir = Path(ca_dir)
        self._validity_days = validity_days
        self._ca_key: ec.EllipticCurvePrivateKey | None = None
        self._ca_cert: x509.Certificate | None = None

    @property
    def cert_path(self) -> Path:
        return self._ca_dir / _CA_CERT_FILE

    @property
    def key_path(self) -> Path:
        return self._ca_dir / _CA_KEY_FILE

    def ensure_ca(self) -> None:
        """Generate the CA key + cert if they don't already exist."""
        self._ca_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        if self.cert_path.exists() and self.key_path.exists():
            self._load_existing()
            logger.info("SandboxCA: loaded existing CA from %s", self._ca_dir)
            return

        self._generate()
        logger.info("SandboxCA: generated new ephemeral CA in %s", self._ca_dir)

    def _load_existing(self) -> None:
        key_pem = self.key_path.read_bytes()
        self._ca_key = serialization.load_pem_private_key(key_pem, password=None)  # type: ignore[assignment]
        cert_pem = self.cert_path.read_bytes()
        self._ca_cert = x509.load_pem_x509_certificate(cert_pem)

    def _generate(self) -> None:
        # ECDSA P-256 — fast key generation, small certs.
        key = ec.generate_private_key(ec.SECP256R1())
        self._ca_key = key

        subject = issuer = x509.Name(
            [
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Squadron Sandbox"),
                x509.NameAttribute(NameOID.COMMON_NAME, "Squadron Ephemeral CA"),
            ]
        )

        now = datetime.datetime.now(datetime.timezone.utc)
        # Backdate not_valid_before by 1 minute to tolerate clock skew
        # between cert generation and first TLS handshake.
        not_before = now - datetime.timedelta(seconds=60)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before)
            .not_valid_after(now + datetime.timedelta(days=self._validity_days))
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=0),
                critical=True,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_cert_sign=True,
                    crl_sign=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(key, hashes.SHA256())
        )
        self._ca_cert = cert

        # Write key (owner-only permissions).
        key_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        self.key_path.write_bytes(key_pem)
        os.chmod(str(self.key_path), 0o600)

        # Write cert (world-readable — injected into agent namespace).
        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        self.cert_path.write_bytes(cert_pem)
        os.chmod(str(self.cert_path), 0o644)

    def sign_leaf(self, hostname: str) -> tuple[bytes, bytes]:
        """Generate a leaf certificate for *hostname*, signed by our CA.

        Returns (cert_pem, key_pem) — both as bytes.
        Used by the MitM proxy to present a valid cert for each upstream.
        """
        if not self._ca_key or not self._ca_cert:
            raise RuntimeError("CA not initialised — call ensure_ca() first")

        leaf_key = ec.generate_private_key(ec.SECP256R1())

        subject = x509.Name(
            [
                x509.NameAttribute(NameOID.COMMON_NAME, hostname),
            ]
        )

        now = datetime.datetime.now(datetime.timezone.utc)
        # Backdate not_valid_before by 1 minute to tolerate clock skew
        not_before = now - datetime.timedelta(seconds=60)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(self._ca_cert.subject)
            .public_key(leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before)
            .not_valid_after(now + datetime.timedelta(days=self._validity_days))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(hostname)]),
                critical=False,
            )
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None),
                critical=True,
            )
            .sign(self._ca_key, hashes.SHA256())
        )

        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        key_pem = leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        return cert_pem, key_pem
