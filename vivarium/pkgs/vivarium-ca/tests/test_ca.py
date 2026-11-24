from __future__ import annotations

import datetime
import ipaddress
import os
import stat
import threading
from typing import TYPE_CHECKING

import pytest
import vivarium_ca
from cryptography import x509
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError
from vivarium_ca import authority, store

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime.datetime(2026, 11, 24, 12, tzinfo=datetime.UTC)


def verify(root: vivarium_ca.CertifiedKey, chain: list[x509.Certificate], name: str) -> None:
    subject: x509.DNSName | x509.IPAddress
    try:
        subject = x509.IPAddress(ipaddress.ip_address(name))
    except ValueError:
        subject = x509.DNSName(name)
    verifier = PolicyBuilder().store(Store([root.certificate])).time(NOW).build_server_verifier(subject)
    verifier.verify(chain[0], chain[1:])


@pytest.fixture(scope="module")
def lab() -> tuple[vivarium_ca.CertifiedKey, vivarium_ca.CertifiedKey]:
    root = authority.root("root", NOW)
    return root, vivarium_ca.intermediate(root, "run", NOW)


@pytest.mark.parametrize(
    "name", ["web.default.svc.cluster.local", "app.test", "x.internal", "localhost", "10.200.0.1", "fd00::1"]
)
def test_a_private_name_verifies(lab: tuple[vivarium_ca.CertifiedKey, vivarium_ca.CertifiedKey], name: str) -> None:
    root, run = lab
    verify(root, [vivarium_ca.leaf(run, [name], NOW).certificate, run.certificate], name)


@pytest.mark.parametrize(
    "names", [["example.com"], ["web.test", "example.com"], ["8.8.8.8"], ["cluster.local.example.com"]]
)
def test_a_public_name_does_not(
    lab: tuple[vivarium_ca.CertifiedKey, vivarium_ca.CertifiedKey], names: list[str]
) -> None:
    root, run = lab
    with pytest.raises(VerificationError):
        verify(root, [vivarium_ca.leaf(run, names, NOW).certificate, run.certificate], names[-1])


def test_the_intermediate_signs_no_ca(lab: tuple[vivarium_ca.CertifiedKey, vivarium_ca.CertifiedKey]) -> None:
    _, run = lab
    constraints = run.certificate.extensions.get_extension_for_class(x509.BasicConstraints)
    assert constraints.critical
    assert constraints.value.path_length == 0
    assert run.certificate.extensions.get_extension_for_class(x509.NameConstraints).critical


def test_the_intermediate_ends_before_its_root() -> None:
    root = authority.root("root", NOW)
    late = NOW + authority.ROOT_LIFETIME - datetime.timedelta(days=2)
    run = vivarium_ca.intermediate(root, "run", late)
    assert run.certificate.not_valid_after_utc <= root.certificate.not_valid_after_utc


def test_a_root_round_trips(tmp_path: Path) -> None:
    made = store.load_or_create(tmp_path / "ca", NOW)
    again = store.load_or_create(tmp_path / "ca", NOW)
    assert again.certificate == made.certificate
    assert stat.S_IMODE(os.stat(tmp_path / "ca" / store.KEY).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "ca").st_mode) == 0o700


def test_a_root_near_its_end_is_replaced(tmp_path: Path) -> None:
    old = store.load_or_create(tmp_path / "ca", NOW)
    late = NOW + authority.ROOT_LIFETIME - authority.INTERMEDIATE_LIFETIME + datetime.timedelta(days=1)
    new = store.load_or_create(tmp_path / "ca", late)
    assert new.certificate != old.certificate
    assert new.certificate.not_valid_after_utc - late >= authority.INTERMEDIATE_LIFETIME
    retired = tmp_path / f"ca-retired-{old.certificate.serial_number:x}"
    assert (retired / store.CERTIFICATE).read_text() == old.certificate_pem()
    assert store.load_or_create(tmp_path / "ca", late).certificate == new.certificate


def test_runs_that_start_at_once_share_one_root(tmp_path: Path) -> None:
    barrier = threading.Barrier(8)
    found: list[x509.Certificate] = []

    def one() -> None:
        barrier.wait()
        found.append(store.load_or_create(tmp_path / "ca", NOW).certificate)

    threads = [threading.Thread(target=one) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(found)) == 1
    assert [path.name for path in tmp_path.iterdir()] == ["ca"]


def test_system_roots_prefers_the_callers_variable(tmp_path: Path) -> None:
    named, fallback = tmp_path / "named.pem", tmp_path / "fallback.pem"
    fallback.write_text("x")
    assert vivarium_ca.system_roots({"SSL_CERT_FILE": str(named)}, [fallback]) == fallback
    named.write_text("x")
    assert vivarium_ca.system_roots({"SSL_CERT_FILE": str(named)}, [fallback]) == named
    assert vivarium_ca.system_roots({}, [tmp_path / "absent"]) is None


def test_bundle_keeps_both() -> None:
    assert vivarium_ca.bundle("A", "B\n") == "A\nB\n"
    assert vivarium_ca.bundle("", "B\n") == "B\n"


def test_bundle_from_an_earlier_bundle_adds_nothing() -> None:
    earlier = vivarium_ca.bundle("A\n", "B\n")
    assert vivarium_ca.bundle(earlier, "B\n") == earlier
