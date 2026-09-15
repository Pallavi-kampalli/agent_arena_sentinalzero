"""Generates TLS Certificate and Private Key for Agent Arena Ingress Reverse Proxy.

Includes Subject Alternative Names (SAN) for:
- arena.localtest.me (publicly resolvable DNS to 127.0.0.1)
- arena.competition.org
- localhost
- 127.0.0.1
"""

import datetime
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ROOT_DIR = Path(__file__).resolve().parent.parent
CERTS_DIR = ROOT_DIR / "certs"


def generate_tls_certificates() -> tuple[Path, Path]:
    CERTS_DIR.mkdir(parents=True, exist_ok=True)
    key_path = CERTS_DIR / "arena.key"
    cert_path = CERTS_DIR / "arena.crt"

    print("Generating 2048-bit RSA Private Key...")
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "California"),
            x509.NameAttribute(NameOID.LOCALITY_NAME, "San Francisco"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Agent Arena Competition Org"),
            x509.NameAttribute(NameOID.COMMON_NAME, "arena.localtest.me"),
        ]
    )

    san_extensions = x509.SubjectAlternativeName(
        [
            x509.DNSName("arena.localtest.me"),
            x509.DNSName("arena.competition.org"),
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
        ]
    )

    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(san_extensions, critical=False)
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None),
            critical=True,
        )
        .sign(private_key, hashes.SHA256())
    )

    # Write private key
    with open(key_path, "wb") as f:
        f.write(
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )

    # Write certificate
    with open(cert_path, "wb") as f:
        f.write(certificate.public_bytes(serialization.Encoding.PEM))

    print(f"[OK] Private Key: {key_path}")
    print(f"[OK] Certificate: {cert_path}")
    return cert_path, key_path


if __name__ == "__main__":
    generate_tls_certificates()
