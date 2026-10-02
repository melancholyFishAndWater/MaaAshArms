"""让 agent 复用客户端（宿主）那份 MaaFramework 原生库（做法参考 MAA1999/M9A 的同名模块）。

发行包不再随 agent 的 Python 运行时携带第二份原生库（win-x64 实测 59.1 MiB）：客户端那份在
``runtimes/<os>-<arch>/native``（tools/install.py 铺的），agent 侧的 ``site-packages/maa/bin``
会被 tools/install.py 的 strip_agent_native_runtime() 删掉。

关键时序：``maa`` 在**导入时**就读 ``MAAFW_BINARY_PATH`` 把库目录定死
（``maa/__init__.py:6-9``、``maa/agent/__init__.py:6-7``），所以本模块必须在任何会 import maa
的模块之前导入并调用。它放在 agent/ 顶层而不是塞进某个包，就是为了这一点——兄弟模块
（base / factory / train …）都会 import maa。

开发态仓库里没有 runtimes/，find_maafw_library_dir() 返回 None，于是不设变量，继续用 wheel
自带的 site-packages/maa/bin，行为与以前一致。
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

ENV_NAME = "MAAFW_BINARY_PATH"

# agent/maafw_paths.py -> agent/ -> 安装根（发行包里就是包根）
DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 与 maa.library.Library.open 的拼接规则一致；agent 侧只需要前两个即可加载
_LIBRARY_NAMES: dict[str, tuple[str, str]] = {
    "win32": ("MaaFramework.dll", "MaaAgentServer.dll"),
    "darwin": ("libMaaFramework.dylib", "libMaaAgentServer.dylib"),
    "linux": ("libMaaFramework.so", "libMaaAgentServer.so"),
}

_PLATFORM_OS: dict[str, str] = {"win32": "win", "darwin": "osx", "linux": "linux"}

_PLATFORM_ARCH: dict[str, str] = {
    "amd64": "x64",
    "x86_64": "x64",
    "arm64": "arm64",
    "aarch64": "arm64",
}


def runtime_platform_tag() -> str | None:
    """runtimes/ 下的平台目录名（win-x64 / osx-arm64 / linux-x64 …），认不出返回 None。"""
    os_name = _PLATFORM_OS.get(sys.platform)
    arch = _PLATFORM_ARCH.get(platform.machine().lower())
    if os_name is None or arch is None:
        return None
    return f"{os_name}-{arch}"


def library_names() -> tuple[str, str] | None:
    """当前平台要加载的框架库与 agent 服务库文件名。"""
    return _LIBRARY_NAMES.get(sys.platform)


def candidate_library_dirs(project_root: Path | None = None) -> list[Path]:
    """客户端原生库的候选目录，按发行包布局排序。"""
    root = DEFAULT_PROJECT_ROOT if project_root is None else Path(project_root)
    candidates: list[Path] = []
    tag = runtime_platform_tag()
    if tag is not None:
        candidates.append(root / "runtimes" / tag / "native")
    # CLI 壳（MaaPiCli）的包把库平铺在包根，放最后只作兜底。
    candidates.append(root)
    return candidates


def find_maafw_library_dir(project_root: Path | None = None) -> Path | None:
    """挑出真正可用的候选目录。

    只判断 exists() 不够：开发机上 ``runtimes/<tag>/native`` 可能是个空目录，指过去要到第一次
    创建 Tasker 时才炸 "Could not find module"。这里要求两个库文件都在。
    """
    names = library_names()
    if names is None:
        return None
    for candidate in candidate_library_dirs(project_root):
        if all((candidate / name).is_file() for name in names):
            return candidate
    return None


def ensure_maafw_binary_path(project_root: Path | str | None = None) -> Path | None:
    """在 import maa 之前调用，返回最终生效的原生库目录。

    已经设过 ``MAAFW_BINARY_PATH`` 的进程一律不动（Android runner 会把它指向 APK 的
    nativeLibraryDir，那里的布局与发行包的 runtimes/ 无关）。返回 None 表示沿用 wheel 自带那份。
    """
    injected = os.environ.get(ENV_NAME, "").strip()
    if injected:
        return Path(injected)

    found = find_maafw_library_dir(Path(project_root) if project_root else None)
    if found is None:
        return None

    os.environ[ENV_NAME] = str(found)
    return found
