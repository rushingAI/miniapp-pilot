"""Provider credentials persisted only through the operating system credential vault."""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path


SERVICE = "MiniApp Pilot Companion"
ACCOUNT = "model-provider"
WINDOWS_TARGET = "MiniAppPilot/model-provider"


class ProviderStoreError(RuntimeError):
    pass


class MacOSKeychainBackend:
    def __init__(self, service=SERVICE, account=ACCOUNT):
        self.service = service
        self.account = account

    def read(self):
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", self.service,
             "-a", self.account, "-w"],
            capture_output=True, text=True,
        )
        return result.stdout.rstrip("\r\n") if result.returncode == 0 else None

    def write(self, secret):
        result = subprocess.run(
            ["/usr/bin/security", "add-generic-password", "-U", "-s", self.service,
             "-a", self.account, "-w", secret],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise ProviderStoreError("无法写入 macOS Keychain")

    def delete(self):
        subprocess.run(
            ["/usr/bin/security", "delete-generic-password", "-s", self.service,
             "-a", self.account],
            capture_output=True, text=True,
        )


class WindowsCredentialBackend:
    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", ctypes.c_uint32),
            ("Type", ctypes.c_uint32),
            ("TargetName", ctypes.c_wchar_p),
            ("Comment", ctypes.c_wchar_p),
            ("LastWrittenLow", ctypes.c_uint32),
            ("LastWrittenHigh", ctypes.c_uint32),
            ("CredentialBlobSize", ctypes.c_uint32),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", ctypes.c_uint32),
            ("AttributeCount", ctypes.c_uint32),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", ctypes.c_wchar_p),
            ("UserName", ctypes.c_wchar_p),
        ]

    def __init__(self, target=WINDOWS_TARGET, account=ACCOUNT):
        self.target = target
        self.account = account
        self.api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        credential_pointer = ctypes.POINTER(self.CREDENTIAL)
        self.api.CredReadW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.POINTER(credential_pointer),
        ]
        self.api.CredReadW.restype = ctypes.c_int
        self.api.CredWriteW.argtypes = [credential_pointer, ctypes.c_uint32]
        self.api.CredWriteW.restype = ctypes.c_int
        self.api.CredDeleteW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
        self.api.CredDeleteW.restype = ctypes.c_int
        self.api.CredFree.argtypes = [ctypes.c_void_p]
        self.api.CredFree.restype = None

    def read(self):
        pointer = ctypes.POINTER(self.CREDENTIAL)()
        if not self.api.CredReadW(self.target, self.CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == self.ERROR_NOT_FOUND:
                return None
            raise ProviderStoreError(f"Windows Credential Manager 读取失败（{error}）")
        try:
            credential = pointer.contents
            raw = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
            return raw.decode("utf-16-le")
        finally:
            self.api.CredFree(pointer)

    def write(self, secret):
        raw = secret.encode("utf-16-le")
        blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        credential = self.CREDENTIAL()
        credential.Type = self.CRED_TYPE_GENERIC
        credential.TargetName = self.target
        credential.CredentialBlobSize = len(raw)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = self.account
        if not self.api.CredWriteW(ctypes.byref(credential), 0):
            error = ctypes.get_last_error()
            raise ProviderStoreError(f"Windows Credential Manager 写入失败（{error}）")

    def delete(self):
        if self.api.CredDeleteW(self.target, self.CRED_TYPE_GENERIC, 0):
            return
        error = ctypes.get_last_error()
        if error != self.ERROR_NOT_FOUND:
            raise ProviderStoreError(f"Windows Credential Manager 删除失败（{error}）")


class UnavailableCredentialBackend:
    def read(self):
        return None

    def write(self, secret):
        raise ProviderStoreError("当前系统不支持安全保存敏感凭据")

    def delete(self):
        return None


def system_credential_backend(*, service=SERVICE, account=ACCOUNT, windows_target=WINDOWS_TARGET):
    if sys.platform == "win32":
        return WindowsCredentialBackend(windows_target, account)
    if sys.platform == "darwin":
        return MacOSKeychainBackend(service, account)
    return UnavailableCredentialBackend()


class CredentialSecretStore:
    """Small secret-only wrapper used by optional settings plugins."""
    def __init__(self, *, service=SERVICE, account=ACCOUNT, windows_target=WINDOWS_TARGET,
                 backend=None):
        self.backend = backend or system_credential_backend(
            service=service, account=account, windows_target=windows_target,
        )

    def load(self):
        return self.backend.read()

    def save(self, secret):
        self.backend.write(secret)

    def delete(self):
        self.backend.delete()


class ProviderStore:
    def __init__(self, settings_path, *, backend=None):
        self.settings_path = Path(settings_path)
        self.backend = backend or system_credential_backend()

    def load(self):
        key = self.backend.read()
        if not key:
            return None
        metadata = {}
        try:
            metadata = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            pass
        return {
            "api_key": key,
            "base_url": str(metadata.get("base_url") or ""),
            "model": str(metadata.get("model") or ""),
        }

    def save(self, api_key, base_url, model):
        self.backend.write(api_key)
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.settings_path.with_suffix(self.settings_path.suffix + ".tmp")
            temporary.write_text(json.dumps({
                "base_url": base_url,
                "model": model,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.settings_path)
        except Exception:
            self.backend.delete()
            raise

    def delete(self):
        self.backend.delete()
        try:
            self.settings_path.unlink()
        except FileNotFoundError:
            pass
