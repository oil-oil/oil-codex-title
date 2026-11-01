"""用真实 Windows 令牌判断 worker 身份，不将派生后台进程当作身份切换。"""
import os
from pathlib import Path


def windows_identity():
    import ctypes
    from ctypes import wintypes
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    userenv = ctypes.WinDLL('userenv', use_last_error=True)
    token = wintypes.HANDLE()
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    userenv.GetUserProfileDirectoryW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [wintypes.HANDLE]
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        sid_string = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_string)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            sid = sid_string.value
        finally:
            kernel.LocalFree(ctypes.cast(sid_string, wintypes.HANDLE))
        length = wintypes.DWORD()
        userenv.GetUserProfileDirectoryW(token, None, ctypes.byref(length))
        profile = ctypes.create_unicode_buffer(length.value)
        if not userenv.GetUserProfileDirectoryW(token, profile, ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        return {'sid': sid, 'profile': profile.value}
    finally:
        kernel.CloseHandle(token)


def owner_process(config):
    if os.name != 'nt':
        return True
    try:
        actual = windows_identity()
        expected = config.get('worker_owner_sid')
        if expected:
            return actual['sid'] == expected
        profile = os.environ.get('USERPROFILE')
        return bool(profile and os.path.normcase(str(Path(profile).resolve())) ==
                    os.path.normcase(str(Path(actual['profile']).resolve())))
    except OSError:
        return False
