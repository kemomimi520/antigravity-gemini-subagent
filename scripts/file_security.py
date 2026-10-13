"""Preserve an existing file's DACL before writing replacement contents.

Only DACL and its inheritance-protection state are copied on Windows. Owner,
SACL, file attributes and other metadata are deliberately outside this API.
The source is read-only; replacement must be a distinct, empty regular file.
All failures stop the caller with OSError; no elevation or permissive fallback.
Non-Windows mode copying is a utility fallback, not Windows ACL enforcement or
a statement that the surrounding Antigravity workflow supports that platform.
"""
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import stat
import struct

WINDOWS = os.name == 'nt'
DACL_SECURITY_INFORMATION = 0x00000004
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
UNPROTECTED_DACL_SECURITY_INFORMATION = 0x20000000
SE_DACL_PROTECTED = 0x1000
ERROR_INSUFFICIENT_BUFFER = 122
MAX_DESCRIPTOR_BYTES = 1024 * 1024


def _load_api():
    api = ctypes.WinDLL('advapi32', use_last_error=True)
    for name, arguments in {
        'GetFileSecurityW': [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p,
                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)],
        'GetSecurityDescriptorControl': [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD),
                                         ctypes.POINTER(wintypes.DWORD)],
        'GetSecurityDescriptorDacl': [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
                                      ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)],
        'IsValidSecurityDescriptor': [ctypes.c_void_p],
        'SetFileSecurityW': [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p],
    }.items():
        function = getattr(api, name)
        function.argtypes = arguments
        function.restype = wintypes.BOOL
    return api


def _api_error(operation):
    # Do not include paths, security descriptor bytes or account names.
    code = ctypes.get_last_error()
    return OSError(code or errno.EIO, operation + ' failed')


@dataclass
class _Security:
    descriptor: object
    protected: bool
    dacl: object


def _read_security(api, path):
    needed = wintypes.DWORD()
    if api.GetFileSecurityW(str(path), DACL_SECURITY_INFORMATION, None, 0, ctypes.byref(needed)):
        raise OSError(errno.EIO, 'Unexpected empty security descriptor')
    if ctypes.get_last_error() != ERROR_INSUFFICIENT_BUFFER:
        raise _api_error('GetFileSecurityW sizing')
    if not 20 <= needed.value <= MAX_DESCRIPTOR_BYTES:
        raise OSError(errno.EIO, 'Invalid security descriptor size')
    buffer = ctypes.create_string_buffer(needed.value)
    if not api.GetFileSecurityW(str(path), DACL_SECURITY_INFORMATION, buffer, len(buffer), ctypes.byref(needed)):
        raise _api_error('GetFileSecurityW')
    if not api.IsValidSecurityDescriptor(buffer):
        raise OSError(errno.EIO, 'Invalid security descriptor')
    control, revision = wintypes.WORD(), wintypes.DWORD()
    if not api.GetSecurityDescriptorControl(buffer, ctypes.byref(control), ctypes.byref(revision)):
        raise _api_error('GetSecurityDescriptorControl')
    present, defaulted = wintypes.BOOL(), wintypes.BOOL()
    dacl = ctypes.c_void_p()
    if not api.GetSecurityDescriptorDacl(buffer, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
        raise _api_error('GetSecurityDescriptorDacl')
    # Distinguish absent, null (unrestricted), and a real ACL. Compare ordered
    # ACE bytes rather than ACL padding, which Windows may normalize on write.
    acl_value = None
    if present.value:
        acl_value = ('null',)
        if dacl.value:
            offset = dacl.value - ctypes.addressof(buffer)
            if offset < 0 or offset + 8 > len(buffer):
                raise OSError(errno.EIO, 'DACL outside security descriptor')
            header = ctypes.string_at(dacl, 8)
            acl_revision, _, acl_size, ace_count, _ = struct.unpack('<BBHHH', header)
            if acl_size < 8 or offset + acl_size > len(buffer):
                raise OSError(errno.EIO, 'Invalid DACL size')
            acl_bytes = ctypes.string_at(dacl, acl_size)
            position, aces = 8, []
            for _ in range(ace_count):
                if position + 4 > acl_size:
                    raise OSError(errno.EIO, 'Invalid ACE header')
                ace_size = struct.unpack_from('<H', acl_bytes, position + 2)[0]
                if ace_size < 4 or position + ace_size > acl_size:
                    raise OSError(errno.EIO, 'Invalid ACE size')
                aces.append(acl_bytes[position:position + ace_size])
                position += ace_size
            acl_value = (acl_revision, tuple(aces))
    return _Security(buffer, bool(control.value & SE_DACL_PROTECTED), acl_value)


def preserve_replacement_security(source, replacement):
    """Copy and verify security on an empty replacement; never modify source.

An absent source needs no security preservation. The caller must create the
empty replacement exclusively and invoke this before writing private bytes.
The paths must stay under the caller's ownership during this operation.
"""
    source, replacement = Path(source), Path(replacement)
    if not source.exists():
        return
    if source.is_symlink() or replacement.is_symlink():
        raise OSError(errno.EINVAL, 'Security preservation refuses symlinks')
    if not source.is_file() or not replacement.is_file():
        raise OSError(errno.EINVAL, 'Security preservation requires regular files')
    if os.path.samefile(source, replacement):
        raise OSError(errno.EINVAL, 'Replacement must be a distinct file')
    if replacement.stat().st_size:
        raise OSError(errno.EINVAL, 'Replacement must be empty before DACL preservation')
    if WINDOWS:
        api = _load_api()
        original = _read_security(api, source)
        information = DACL_SECURITY_INFORMATION | (
            PROTECTED_DACL_SECURITY_INFORMATION if original.protected
            else UNPROTECTED_DACL_SECURITY_INFORMATION)
        if not api.SetFileSecurityW(str(replacement), information, original.descriptor):
            raise _api_error('SetFileSecurityW')
        observed = _read_security(api, replacement)
        unchanged = _read_security(api, source)
        if original.protected != observed.protected or original.dacl != observed.dacl:
            raise OSError(errno.EIO, 'Replacement DACL readback mismatch')
        if original.protected != unchanged.protected or original.dacl != unchanged.dacl:
            raise OSError(errno.EIO, 'Source DACL changed during preservation')
    else:
        mode = stat.S_IMODE(source.stat().st_mode)
        os.chmod(replacement, mode)
        if stat.S_IMODE(replacement.stat().st_mode) != mode:
            raise OSError(errno.EIO, 'Replacement mode readback mismatch')


def protect_new_private_file(path):
    """Protect an owned empty file before its first private contents are written."""
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size:
        raise OSError(errno.EINVAL, 'Private file protection requires an empty regular file')
    if not WINDOWS:
        os.chmod(path, 0o600)
        return
    api = _load_api()
    convert = api.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    convert.restype = wintypes.BOOL
    descriptor = ctypes.c_void_p()
    # Owner Rights grants the creator access; SYSTEM retains its normal access.
    if not convert('D:P(A;;FA;;;OW)(A;;FA;;;SY)', 1, ctypes.byref(descriptor), None):
        raise _api_error('Private security descriptor conversion')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        if not api.SetFileSecurityW(str(path), DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION, descriptor):
            raise _api_error('Private file security')
        observed = _read_security(api, path)
        if not observed.protected or observed.dacl is None or observed.dacl == ('null',):
            raise OSError(errno.EIO, 'Private file DACL readback failed')
    finally:
        kernel.LocalFree(descriptor)
