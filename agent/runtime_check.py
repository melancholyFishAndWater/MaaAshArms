"""Windows agent startup checks; standard library only, before importing maa."""

from __future__ import annotations

import ctypes
import os
import sys


def windows_integrity() -> str:
    """Read the current token rather than infer it from executable file labels."""
    from ctypes import wintypes

    api = ctypes.WinDLL("advapi32", use_last_error=True)
    query = api.GetTokenInformation
    query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                      wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    query.restype = wintypes.BOOL
    size = wintypes.DWORD()
    token = wintypes.HANDLE(-4)  # GetCurrentProcessToken pseudo-handle (Windows 8+).
    query(token, 25, None, 0, ctypes.byref(size))  # TokenIntegrityLevel
    if not size.value:
        raise ctypes.WinError(ctypes.get_last_error())
    data = ctypes.create_string_buffer(size.value)
    if not query(token, 25, data, size, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    sid = ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0]
    if sid is None:
        raise OSError("TokenIntegrityLevel returned no SID")
    count = ctypes.c_ubyte.from_address(sid + 1).value
    if not count:
        raise OSError("TokenIntegrityLevel returned an empty SID")
    rid = ctypes.c_uint32.from_address(sid + 8 + 4 * (count - 1)).value
    return ("Untrusted" if rid < 4096 else "Low" if rid < 8192 else
            "Medium" if rid < 12288 else "High" if rid < 16384 else "System")


def windows_temp_probe() -> tuple[bool, str, int]:
    """Match libzmq's Windows _wtmpnam_s/_wmkdir path, not tempfile fallback."""
    crt = ctypes.CDLL("ucrtbase", use_errno=True)
    name = crt._wtmpnam_s
    name.argtypes = [ctypes.c_wchar_p, ctypes.c_size_t]
    name.restype = ctypes.c_int
    mkdir = crt._wmkdir
    mkdir.argtypes = [ctypes.c_wchar_p]
    mkdir.restype = ctypes.c_int
    buf = ctypes.create_unicode_buffer(260)
    result = name(buf, len(buf))
    if result:
        return False, buf.value, result
    ctypes.set_errno(0)
    if mkdir(buf.value) != 0:
        return False, buf.value, ctypes.get_errno()
    # Only remove the empty directory this invocation successfully created.
    os.rmdir(buf.value)
    return True, buf.value, 0


def ensure_runtime_ready() -> None:
    if sys.platform != "win32":
        return
    try:
        integrity = windows_integrity()
    except OSError as exc:
        integrity = f"unknown ({exc})"
    try:
        ok, path, error = windows_temp_probe()
    except OSError as exc:
        ok, path, error = False, str(exc), exc.errno or 0
    if ok:
        return
    base = getattr(sys, "_base_executable", sys.executable)
    raise SystemExit(
        "agent 启动检查失败：无法创建或清理 ZeroMQ 所需的 Windows 临时目录。\n"
        f"进程完整性：{integrity}\n解释器：{sys.executable}\n基础解释器：{base}\n"
        f"临时路径／错误：{path}\nerrno：{error}\n"
        "若为 Low/Untrusted，请检查 uv、venv 启动器及基础 python.exe 的完整性标签。\n"
        "开发环境可先运行 tools/dev/python_integrity.ps1 检查；不要递归修改项目或系统 Temp 权限。"
    )
