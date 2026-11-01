"""通过 Windows 用户环境变量保存中转站密钥；不经 shell 或插件配置。"""
import os
import re
import sys

ENVIRONMENT_SUBKEY = "Environment"
RESERVED_NAMES = {"PATH", "PATHEXT", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP",
                  "SYSTEMROOT", "WINDIR", "COMSPEC", "PYTHONPATH", "PYTHONHOME", "CODEX_HOME",
                  "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_APP_TOOLS_PIPE_PATH", "OIL_CODEX_TITLE_DATA",
                  "OIL_CODEX_TITLE_WORKER", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}


def validate_key_input(name, value):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name):
        raise ValueError("请填写有效的密钥环境变量名")
    if name.upper() in RESERVED_NAMES:
        raise ValueError("此变量用于系统或运行环境，请为中转站密钥使用独立变量名")
    if not isinstance(value, str) or not 1 <= len(value) <= 4096 or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("密钥不能为空、超过 4096 字符或包含空白及控制字符")


def notify_environment_change():
    """有界通知桌面环境更新，新启动的程序可继承新值。"""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    send = user32.SendMessageTimeoutW
    send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                     wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    send.restype = wintypes.LPARAM
    area = ctypes.create_unicode_buffer("Environment")
    result = ctypes.c_size_t()
    return bool(send(0xffff, 0x001a, 0, ctypes.cast(area, ctypes.c_void_p).value,
                     0x0002, 500, ctypes.byref(result)))


def persist_user_key(name, value):
    validate_key_input(name, value)
    if sys.platform != "win32":
        raise ValueError("对话框保存密钥目前仅支持 Windows，请通过系统环境变量设置密钥")
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, ENVIRONMENT_SUBKEY, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        stored, _ = winreg.QueryValueEx(key, name)
        if stored != value:
            raise OSError("用户环境变量写入核验失败")
    # 仅在持久化成功后更新设置服务自身，不影响已运行的 Codex 进程。
    os.environ[name] = value
    try:
        notify_environment_change()
    except OSError:
        # 通知失败不推翻已经核验的持久化结果；用户仍需完全重启 Codex。
        pass
