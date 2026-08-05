#!/usr/bin/env python3
"""Store impersonated Google ADC in macOS Keychain and materialize it for ADC.

Keychain cannot be mounted into a Linux container. The ``materialize`` command
therefore writes a mode-0600 cache file whose path can be exported by .envrc and
mounted read-only by Docker Compose. The durable source remains in Keychain.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

DEFAULT_SERVICE = "ai-gateway-google-dlp-adc"
ERR_SEC_ITEM_NOT_FOUND = -25300


class KeychainError(RuntimeError):
    pass


def adc_service_account(data: bytes) -> str:
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("ADC content is not valid JSON") from None
    if not isinstance(payload, dict) or payload.get("type") != "impersonated_service_account":
        raise ValueError("ADC must use type=impersonated_service_account")
    source_credentials = payload.get("source_credentials")
    if not isinstance(source_credentials, dict) or source_credentials.get("type") not in {
        "authorized_user",
        "external_account",
    }:
        raise ValueError("ADC source must be user or federated credentials, never a service-account key")
    if any(key in source_credentials for key in ("private_key", "private_key_id")):
        raise ValueError("ADC source contains forbidden private-key material")
    url = payload.get("service_account_impersonation_url")
    if not isinstance(url, str):
        raise TypeError("ADC does not name an impersonated service account")
    parsed = urlparse(url)
    prefix = "/v1/projects/-/serviceAccounts/"
    suffix = ":generateAccessToken"
    if (
        parsed.scheme != "https"
        or parsed.netloc != "iamcredentials.googleapis.com"
        or not parsed.path.startswith(prefix)
        or not parsed.path.endswith(suffix)
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("ADC uses an untrusted service-account impersonation URL")
    principal = unquote(parsed.path[len(prefix):-len(suffix)])
    if not principal.endswith(".iam.gserviceaccount.com"):
        raise ValueError("ADC impersonation target is not a service-account email")
    return principal


def validate_adc(data: bytes, expected_service_account: str | None) -> str:
    principal = adc_service_account(data)
    if expected_service_account and principal != expected_service_account:
        raise ValueError("ADC impersonation target does not match the expected service account")
    return principal


def materialize_adc(data: bytes, destination: Path) -> None:
    parent = destination.parent
    if parent.is_symlink():
        raise ValueError("ADC destination directory must not be a symlink")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stat.S_IMODE(parent.stat().st_mode) & 0o077:
        raise ValueError("ADC destination directory must have mode 0700 or stricter")
    fd, temporary_name = tempfile.mkstemp(prefix=".adc-", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
        os.chmod(destination, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(temporary_name).unlink(missing_ok=True)
        raise


class MacOSKeychain:
    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise KeychainError("macOS Keychain is only available on macOS")
        self._security = ctypes.CDLL(
            "/System/Library/Frameworks/Security.framework/Security"
        )
        self._core_foundation = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        uint32 = ctypes.c_uint32
        void_p = ctypes.c_void_p
        char_p = ctypes.c_char_p
        self._security.SecKeychainFindGenericPassword.argtypes = [
            void_p, uint32, char_p, uint32, char_p,
            ctypes.POINTER(uint32), ctypes.POINTER(void_p), ctypes.POINTER(void_p),
        ]
        self._security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self._security.SecKeychainAddGenericPassword.argtypes = [
            void_p, uint32, char_p, uint32, char_p, uint32, void_p, ctypes.POINTER(void_p),
        ]
        self._security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        self._security.SecKeychainItemModifyAttributesAndData.argtypes = [
            void_p, void_p, uint32, void_p,
        ]
        self._security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        self._security.SecKeychainItemFreeContent.argtypes = [void_p, void_p]
        self._security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        self._core_foundation.CFRelease.argtypes = [void_p]
        self._core_foundation.CFRelease.restype = None

    @staticmethod
    def _encoded(value: str) -> bytes:
        return value.encode("utf-8")

    def _find(self, service: str, account: str) -> tuple[int, bytes | None, ctypes.c_void_p]:
        service_bytes = self._encoded(service)
        account_bytes = self._encoded(account)
        length = ctypes.c_uint32()
        data = ctypes.c_void_p()
        item = ctypes.c_void_p()
        status = self._security.SecKeychainFindGenericPassword(
            None,
            len(service_bytes), service_bytes,
            len(account_bytes), account_bytes,
            ctypes.byref(length), ctypes.byref(data), ctypes.byref(item),
        )
        if status != 0:
            return status, None, item
        try:
            value = ctypes.string_at(data, length.value)
        finally:
            self._security.SecKeychainItemFreeContent(None, data)
        return status, value, item

    def get(self, service: str, account: str) -> bytes:
        status, value, item = self._find(service, account)
        try:
            if status == ERR_SEC_ITEM_NOT_FOUND:
                raise KeychainError(f"Keychain item {service!r} was not found")
            if status != 0 or value is None:
                raise KeychainError(f"Keychain read failed with OSStatus {status}")
            return value
        finally:
            if item:
                self._core_foundation.CFRelease(item)

    def set(self, service: str, account: str, value: bytes) -> None:
        status, _, item = self._find(service, account)
        value_buffer = ctypes.create_string_buffer(value)
        try:
            if status == 0:
                result = self._security.SecKeychainItemModifyAttributesAndData(
                    item, None, len(value), value_buffer
                )
            elif status == ERR_SEC_ITEM_NOT_FOUND:
                service_bytes = self._encoded(service)
                account_bytes = self._encoded(account)
                result = self._security.SecKeychainAddGenericPassword(
                    None,
                    len(service_bytes), service_bytes,
                    len(account_bytes), account_bytes,
                    len(value), value_buffer,
                    None,
                )
            else:
                raise KeychainError(f"Keychain lookup failed with OSStatus {status}")
            if result != 0:
                raise KeychainError(f"Keychain write failed with OSStatus {result}")
        finally:
            if item:
                self._core_foundation.CFRelease(item)


def default_destination() -> Path:
    return Path.home() / "Library" / "Caches" / "ai-gateway" / "google-dlp-adc.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    parser.add_argument("--account", default=os.environ.get("USER", "ai-gateway"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    store = subparsers.add_parser("store", help="Store an impersonated ADC JSON file")
    store.add_argument("--source", type=Path, required=True)
    store.add_argument("--expected-service-account")
    store.add_argument(
        "--remove-source",
        action="store_true",
        help="unlink the source only after Keychain readback matches",
    )

    materialize = subparsers.add_parser(
        "materialize", help="Write a mode-0600 ADC cache file and print its path"
    )
    materialize.add_argument("--destination", type=Path, default=default_destination())
    materialize.add_argument("--expected-service-account")

    status = subparsers.add_parser("status", help="Validate the item without printing it")
    status.add_argument("--expected-service-account")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        keychain = MacOSKeychain()
        if args.command == "store":
            data = args.source.read_bytes()
            principal = validate_adc(data, args.expected_service_account)
            keychain.set(args.service, args.account, data)
            if keychain.get(args.service, args.account) != data:
                raise KeychainError("Keychain verification failed; source was preserved")
            if args.remove_source:
                args.source.unlink()
            print(f"Stored impersonated ADC for {principal} in macOS Keychain")
            return 0

        data = keychain.get(args.service, args.account)
        principal = validate_adc(data, args.expected_service_account)
        if args.command == "status":
            print(f"Keychain ADC is valid for {principal}")
            return 0
        materialize_adc(data, args.destination)
        print(args.destination.resolve())
        return 0
    except (KeychainError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
