"""macOS Keychain storage using native APIs; secrets never enter process arguments."""
import ctypes as c
import json
import sys

SERVICE = b"trainerroad-fuelin-sync"
ACCOUNT = b"default-session"


class StorageError(RuntimeError):
    pass


class KeychainStore:
    def __init__(self):
        if sys.platform != "darwin":
            raise StorageError("Persistent authentication currently requires macOS Keychain")
        self.sec = c.CDLL("/System/Library/Frameworks/Security.framework/Security")
        self.cf = c.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.cf.CFRelease.argtypes = [c.c_void_p]
        signatures = {
            "SecKeychainFindGenericPassword": [c.c_void_p, c.c_uint32, c.c_void_p, c.c_uint32, c.c_void_p, c.POINTER(c.c_uint32), c.POINTER(c.c_void_p), c.POINTER(c.c_void_p)],
            "SecKeychainAddGenericPassword": [c.c_void_p, c.c_uint32, c.c_void_p, c.c_uint32, c.c_void_p, c.c_uint32, c.c_void_p, c.POINTER(c.c_void_p)],
            "SecKeychainItemModifyAttributesAndData": [c.c_void_p, c.c_void_p, c.c_uint32, c.c_void_p],
            "SecKeychainItemDelete": [c.c_void_p],
            "SecKeychainItemFreeContent": [c.c_void_p, c.c_void_p],
        }
        for name, args in signatures.items():
            function = getattr(self.sec, name)
            function.argtypes, function.restype = args, c.c_int32

    def _find(self):
        length, data, item = c.c_uint32(), c.c_void_p(), c.c_void_p()
        status = self.sec.SecKeychainFindGenericPassword(None, len(SERVICE), SERVICE, len(ACCOUNT), ACCOUNT,
                                                       c.byref(length), c.byref(data), c.byref(item))
        if status == -25300:
            return None, None
        if status != 0:
            raise StorageError(f"Keychain read failed (OSStatus {status})")
        try:
            value = c.string_at(data, length.value)
        finally:
            self.sec.SecKeychainItemFreeContent(None, data)
        return value, item

    def load(self):
        raw, item = self._find()
        if raw is None:
            return None
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or any(not isinstance(value.get(key), str) or not value[key] for key in ("email", "token", "user_id")):
                raise ValueError()
            return value
        except (ValueError, UnicodeDecodeError):
            raise StorageError("Saved Keychain session is invalid; run python3 auth.py logout") from None
        finally:
            self.cf.CFRelease(item)

    def save(self, value):
        raw = json.dumps(value).encode()
        _, item = self._find()
        try:
            if item:
                status = self.sec.SecKeychainItemModifyAttributesAndData(item, None, len(raw), raw)
            else:
                status = self.sec.SecKeychainAddGenericPassword(None, len(SERVICE), SERVICE, len(ACCOUNT), ACCOUNT, len(raw), raw, None)
            if status != 0:
                raise StorageError(f"Keychain save failed (OSStatus {status})")
        finally:
            if item:
                self.cf.CFRelease(item)

    def delete(self):
        _, item = self._find()
        if item:
            try:
                status = self.sec.SecKeychainItemDelete(item)
                if status != 0:
                    raise StorageError(f"Keychain delete failed (OSStatus {status})")
            finally:
                self.cf.CFRelease(item)
