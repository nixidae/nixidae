"""Certificates of a lab CA: a root per user, an intermediate per run, and
leaves.

Both CAs carry the same name constraints: names and addresses that no
public CA can issue for. A lab holds only the intermediate's key, so a lab
that leaks it lets nobody impersonate a real site to a host that trusts the
root. OpenSSL 3.5 and Go 1.27 refuse a leaf for `example.com` or `8.8.8.8`
from either CA (measured 2026-11-24, nixidae/nixidae#1584).
"""

from __future__ import annotations

import datetime
import ipaddress
from dataclasses import dataclass
from typing import Final

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

PERMITTED_NAMES: Final = ("cluster.local", "test", "internal", "localhost")
"""DNS suffixes: a Kubernetes cluster's domain and the names RFC 2606,
RFC 6761 and ICANN keep out of the public DNS."""

PERMITTED_NETWORKS: Final = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.0/8",
    "fc00::/7",
    "::1/128",
)

ROOT_LIFETIME: Final = datetime.timedelta(days=3650)
"""Long: a person imports the root into a browser once."""

INTERMEDIATE_LIFETIME: Final = datetime.timedelta(days=30)
"""Longer than an environment that `vivarium up` keeps for days."""

LEAF_LIFETIME: Final = datetime.timedelta(days=7)

BACKDATE: Final = datetime.timedelta(hours=1)
"""A guest's clock may run behind the host's."""


@dataclass(frozen=True, slots=True)
class CertifiedKey:
    """A certificate and its private key: a CA's or a server's."""

    certificate: x509.Certificate
    key: ec.EllipticCurvePrivateKey

    def certificate_pem(self) -> str:
        return certificate_pem(self.certificate)

    def key_pem(self) -> str:
        return self.key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()


def certificate_pem(certificate: x509.Certificate) -> str:
    return certificate.public_bytes(serialization.Encoding.PEM).decode()


def load(certificate_pem: str, key_pem: str) -> CertifiedKey:
    key = serialization.load_pem_private_key(key_pem.encode(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError(f"a lab CA's key is an EC key, not {type(key).__name__}")
    return CertifiedKey(x509.load_pem_x509_certificate(certificate_pem.encode()), key)


def constraints() -> x509.NameConstraints:
    permitted: list[x509.GeneralName] = [x509.DNSName(name) for name in PERMITTED_NAMES]
    permitted += [x509.IPAddress(ipaddress.ip_network(network)) for network in PERMITTED_NETWORKS]
    return x509.NameConstraints(permitted_subtrees=permitted, excluded_subtrees=None)


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _ca_usage() -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=False,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=True,
        crl_sign=True,
        encipher_only=False,
        decipher_only=False,
    )


def _builder(
    subject: x509.Name,
    issuer: x509.Name,
    key: ec.EllipticCurvePrivateKey,
    now: datetime.datetime,
    lifetime: datetime.timedelta,
) -> x509.CertificateBuilder:
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - BACKDATE)
        .not_valid_after(now + lifetime)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    )


def root(common_name: str, now: datetime.datetime) -> CertifiedKey:
    """A self-signed root that may sign CAs, which may not sign CAs."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = _name(common_name)
    certificate = (
        _builder(subject, subject, key, now, ROOT_LIFETIME)
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .add_extension(_ca_usage(), critical=True)
        .add_extension(constraints(), critical=True)
        .sign(key, hashes.SHA256())
    )
    return CertifiedKey(certificate, key)


def intermediate(parent: CertifiedKey, common_name: str, now: datetime.datetime) -> CertifiedKey:
    """A CA for one run, which signs leaves only."""
    key = ec.generate_private_key(ec.SECP256R1())
    lifetime = min(INTERMEDIATE_LIFETIME, parent.certificate.not_valid_after_utc - now)
    certificate = (
        _builder(_name(common_name), parent.certificate.subject, key, now, lifetime)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(_ca_usage(), critical=True)
        .add_extension(constraints(), critical=True)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(parent.key.public_key()), critical=False)
        .sign(parent.key, hashes.SHA256())
    )
    return CertifiedKey(certificate, key)


def leaf(parent: CertifiedKey, names: list[str], now: datetime.datetime) -> CertifiedKey:
    """A server certificate for DNS names and IP addresses."""
    alternatives: list[x509.GeneralName] = []
    for name in names:
        try:
            alternatives.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            alternatives.append(x509.DNSName(name))
    key = ec.generate_private_key(ec.SECP256R1())
    certificate = (
        _builder(_name(names[0]), parent.certificate.subject, key, now, LEAF_LIFETIME)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName(alternatives), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(parent.key.public_key()), critical=False)
        .sign(parent.key, hashes.SHA256())
    )
    return CertifiedKey(certificate, key)
