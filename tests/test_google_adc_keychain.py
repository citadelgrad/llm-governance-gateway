from __future__ import annotations

import ctypes
import importlib.util
import json
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "google_adc_keychain.py"
SPEC = importlib.util.spec_from_file_location("google_adc_keychain", SCRIPT)
assert SPEC and SPEC.loader
keychain = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(keychain)


def _adc(principal: str = "gateway-dlp@example.iam.gserviceaccount.com") -> bytes:
    return json.dumps(
        {
            "type": "impersonated_service_account",
            "service_account_impersonation_url": (
                "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
                f"{principal}:generateAccessToken"
            ),
            "source_credentials": {"type": "authorized_user", "refresh_token": "secret"},
        }
    ).encode()


def test_validate_adc_returns_impersonated_principal_without_exposing_source_secret():
    assert keychain.validate_adc(
        _adc(), "gateway-dlp@example.iam.gserviceaccount.com"
    ) == "gateway-dlp@example.iam.gserviceaccount.com"


def test_validate_adc_rejects_personal_user_adc():
    with pytest.raises(ValueError, match="type=impersonated_service_account"):
        keychain.validate_adc(
            json.dumps({"type": "authorized_user", "refresh_token": "secret"}).encode(),
            None,
        )


def test_validate_adc_rejects_wrong_impersonation_target():
    with pytest.raises(ValueError, match="does not match"):
        keychain.validate_adc(_adc(), "other@example.iam.gserviceaccount.com")


def test_validate_adc_rejects_nested_service_account_private_key_source():
    payload = json.loads(_adc())
    payload["source_credentials"] = {
        "type": "service_account",
        "client_email": "source@example.iam.gserviceaccount.com",
        "private_key": "forbidden",
    }

    with pytest.raises(ValueError, match="never a service-account key"):
        keychain.validate_adc(json.dumps(payload).encode(), None)


def test_validate_adc_rejects_untrusted_impersonation_url():
    payload = json.loads(_adc())
    payload["service_account_impersonation_url"] = (
        "https://attacker.example/v1/projects/-/serviceAccounts/"
        "gateway-dlp@example.iam.gserviceaccount.com:generateAccessToken"
    )

    with pytest.raises(ValueError, match="untrusted"):
        keychain.validate_adc(json.dumps(payload).encode(), None)


def test_materialize_adc_writes_private_atomic_file(tmp_path: Path):
    destination = tmp_path / "cache" / "adc.json"

    keychain.materialize_adc(_adc(), destination)

    assert destination.read_bytes() == _adc()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700


def test_materialize_adc_rejects_world_searchable_destination_directory(tmp_path: Path):
    destination = tmp_path / "public-cache" / "adc.json"
    destination.parent.mkdir(mode=0o755)

    with pytest.raises(ValueError, match="mode 0700"):
        keychain.materialize_adc(_adc(), destination)


def test_keychain_get_releases_retained_item_reference():
    class FakeCoreFoundation:
        def __init__(self) -> None:
            self.released: list[int] = []

        def CFRelease(self, item) -> None:
            self.released.append(item.value)

    client = object.__new__(keychain.MacOSKeychain)
    client._core_foundation = FakeCoreFoundation()
    client._find = lambda _service, _account: (0, b"value", ctypes.c_void_p(123))

    assert client.get("service", "account") == b"value"
    assert client._core_foundation.released == [123]


def test_keychain_set_releases_retained_item_reference():
    class FakeSecurity:
        def SecKeychainItemModifyAttributesAndData(
            self, _item, _attributes, _length, _value
        ) -> int:
            return 0

    class FakeCoreFoundation:
        def __init__(self) -> None:
            self.released: list[int] = []

        def CFRelease(self, item) -> None:
            self.released.append(item.value)

    client = object.__new__(keychain.MacOSKeychain)
    client._security = FakeSecurity()
    client._core_foundation = FakeCoreFoundation()
    client._find = lambda _service, _account: (0, b"old", ctypes.c_void_p(456))

    client.set("service", "account", b"new")

    assert client._core_foundation.released == [456]


def test_store_removes_source_only_after_verified_keychain_readback(
    monkeypatch, tmp_path: Path
):
    class FakeKeychain:
        value = b""

        def set(self, _service: str, _account: str, value: bytes) -> None:
            self.value = value

        def get(self, _service: str, _account: str) -> bytes:
            return self.value

    source = tmp_path / "adc.json"
    source.write_bytes(_adc())
    fake = FakeKeychain()
    monkeypatch.setattr(keychain, "MacOSKeychain", lambda: fake)

    result = keychain.main(
        [
            "store",
            "--source",
            str(source),
            "--expected-service-account",
            "gateway-dlp@example.iam.gserviceaccount.com",
            "--remove-source",
        ]
    )

    assert result == 0
    assert fake.value == _adc()
    assert not source.exists()
