"""Native credential stores only; never fall back to a file or subprocess."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys
from typing import Protocol


class CredentialError(RuntimeError):
    pass


class CredentialStore(Protocol):
    def load(self, key: str) -> bytes | None: ...
    def save(self, key: str, value: bytes) -> None: ...
    def delete(self, key: str) -> None: ...


SERVICE = "tv.vlc-monitor.wechat.trial"


class MacKeychainStore:
    def __init__(self):
        self.lib = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        self.cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        u32, ptr = ctypes.c_uint32, ctypes.c_void_p
        self.lib.SecKeychainFindGenericPassword.argtypes = [ptr, u32, ptr, u32, ptr, ctypes.POINTER(u32), ctypes.POINTER(ptr), ctypes.POINTER(ptr)]
        self.lib.SecKeychainAddGenericPassword.argtypes = [ptr, u32, ptr, u32, ptr, u32, ptr, ctypes.POINTER(ptr)]
        self.lib.SecKeychainItemModifyAttributesAndData.argtypes = [ptr, ptr, u32, ptr]
        self.lib.SecKeychainItemDelete.argtypes = [ptr]
        self.lib.SecKeychainItemFreeContent.argtypes = [ptr, ptr]
        self.cf.CFRelease.argtypes = [ptr]
        self.cf.CFRelease.restype = None
        for name in ("SecKeychainFindGenericPassword", "SecKeychainAddGenericPassword", "SecKeychainItemModifyAttributesAndData", "SecKeychainItemDelete", "SecKeychainItemFreeContent"):
            getattr(self.lib, name).restype = ctypes.c_int32

    def _find(self, key):
        service, account = SERVICE.encode(), key.encode()
        length, data, item = ctypes.c_uint32(), ctypes.c_void_p(), ctypes.c_void_p()
        status = self.lib.SecKeychainFindGenericPassword(None, len(service), service, len(account), account, ctypes.byref(length), ctypes.byref(data), ctypes.byref(item))
        if status == -25300:
            return None, None
        if status != 0:
            raise CredentialError("System credential access denied or unavailable")
        try:
            return ctypes.string_at(data, length.value), item
        finally:
            self.lib.SecKeychainItemFreeContent(None, data)

    def load(self, key):
        value, item = self._find(key)
        if item:
            self.cf.CFRelease(item)
        return value

    def save(self, key, value):
        _, item = self._find(key)
        if item:
            try:
                status = self.lib.SecKeychainItemModifyAttributesAndData(item, None, len(value), value)
            finally:
                self.cf.CFRelease(item)
        else:
            service, account = SERVICE.encode(), key.encode()
            status = self.lib.SecKeychainAddGenericPassword(None, len(service), service, len(account), account, len(value), value, None)
        if status != 0:
            raise CredentialError("System credential write failed")

    def delete(self, key):
        _, item = self._find(key)
        if not item:
            return
        try:
            status = self.lib.SecKeychainItemDelete(item)
        finally:
            self.cf.CFRelease(item)
        if status != 0:
            raise CredentialError("System credential deletion failed")


class WindowsCredentialStore:
    def __init__(self):
        class Credential(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]
        self.Credential = Credential
        self.lib = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self.lib.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(Credential))]
        self.lib.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
        self.lib.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self.lib.CredFree.argtypes = [ctypes.c_void_p]
        self.lib.CredFree.restype = None
        for name in ("CredReadW", "CredWriteW", "CredDeleteW"):
            getattr(self.lib, name).restype = wintypes.BOOL

    def load(self, key):
        pointer = ctypes.POINTER(self.Credential)()
        if not self.lib.CredReadW(f"{SERVICE}/{key}", 1, 0, ctypes.byref(pointer)):
            if ctypes.get_last_error() == 1168:
                return None
            raise CredentialError("System credential access denied or unavailable")
        try:
            return ctypes.string_at(pointer.contents.CredentialBlob, pointer.contents.CredentialBlobSize)
        finally:
            self.lib.CredFree(pointer)

    def save(self, key, value):
        # Windows generic credentials have a 2560-byte blob limit. Do not split/fallback.
        if len(value) > 2560:
            raise CredentialError("Binding exceeds native credential capacity")
        buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        credential = self.Credential()
        credential.Type = 1
        credential.TargetName = f"{SERVICE}/{key}"
        credential.CredentialBlobSize = len(value)
        credential.CredentialBlob = buffer
        credential.Persist = 2
        credential.UserName = "local-binding"
        if not self.lib.CredWriteW(ctypes.byref(credential), 0):
            raise CredentialError("System credential write failed")

    def delete(self, key):
        if not self.lib.CredDeleteW(f"{SERVICE}/{key}", 1, 0) and ctypes.get_last_error() != 1168:
            raise CredentialError("System credential deletion failed")


def system_credential_store() -> CredentialStore:
    try:
        if sys.platform == "darwin":
            return MacKeychainStore()
        if sys.platform == "win32":
            return WindowsCredentialStore()
    except (OSError, AttributeError):
        raise CredentialError("Native credential backend unavailable") from None
    raise CredentialError("No supported native credential backend; plaintext fallback forbidden")


class GuardedCredentialStore:
    """A nonsensitive revocation tombstone survives failed cleanup and app restarts."""
    def __init__(self, factory, marker):
        self.factory = factory
        self.marker = marker
        self.native = None

    def _backend(self):
        if self.native is None:
            self.native = self.factory()
        return self.native

    def load(self, key):
        if self.marker.exists():
            raise CredentialError("Previous credential cleanup requires retry")
        return self._backend().load(key)

    def save(self, key, value):
        if self.marker.exists():
            raise CredentialError("Previous credential cleanup requires retry")
        self._backend().save(key, value)

    def delete(self, key):
        try:
            self.marker.parent.mkdir(parents=True, exist_ok=True)
            # Contains no account, token, context, QR or challenge material.
            with self.marker.open("wb") as stream:
                stream.write(b"cleanup-required\n")
                stream.flush()
                import os
                os.fsync(stream.fileno())
            native = self._backend()
            native.delete(key)
            if native.load(key) is not None:
                raise CredentialError("System credential deletion could not be verified")
            self.marker.unlink()
        except OSError:
            raise CredentialError("Credential revocation marker could not be updated") from None
