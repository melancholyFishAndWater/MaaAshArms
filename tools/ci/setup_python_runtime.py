#!/usr/bin/env python3
"""
本文件由deepseek-v4.1-flash生成，未经过人工审阅。

给发布包准备一份"随身 Python"：解释器 + agent 依赖。

产物（`--dest` 目录里的内容，将来会被整目录拷成发布包的 `python/`）：
    win-*  ：python.exe / python313._pth / Lib/site-packages/{maa,numpy,strenum,MaaAgentBinary}
    其它   ：bin/python3 / lib/python3.13/site-packages/{maa,numpy,strenum,MaaAgentBinary}

用法：
    python tools/ci/setup_python_runtime.py --platform win-x64 --dest .python-runtime \
        --requirements requirements.txt --maafw-version v5.12.2

设计取舍见 .agents/project.md 的「发布包自带的 Python 运行时」。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
import http.client
import io
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# 段 0：stdout 编码兜底
#
# Windows runner 的控制台编码是 cp1252，Python 默认拿它当 stdout/stderr 编码，print 中文就
# UnicodeEncodeError（本机 cp936 不复现，CI 的 windows 档每一句中文都会撞一次）。
# 只调 errors 不动 encoding：本机保持原编码、不乱码；编码装不下时退化成替代字符而不是崩。
# CI 侧另有 job 级 PYTHONUTF8=1，日志按 UTF-8 输出，中文正常显示。
# ---------------------------------------------------------------------------
for _stream in (sys.stdout, sys.stderr):
    # reconfigure() 定义在 io.TextIOWrapper 上，而 sys.stdout 的静态类型是 typing.TextIO，
    # 直接调用会被 Pylance 报 reportAttributeAccessIssue；isinstance 收窄后静态与运行时都成立。
    if isinstance(_stream, io.TextIOWrapper):
        try:
            _stream.reconfigure(errors="replace")
        except (OSError, ValueError):
            pass

# ---------------------------------------------------------------------------
# 段 1：常量。所有"平台 → 资源/标签"的映射都集中在这里，别处不再出现字面量。
# ---------------------------------------------------------------------------

# Windows 用 python.org 的 embeddable 包；Mac/Linux 用 python-build-standalone。
# 两边都是 3.13：MaaFw 只要求 >=3.9，3.13 有现成的 numpy 轮子。
PYTHON_EMBED_VERSION = "3.13.14"
PYTHON_MINOR = "3.13"

EMBED_ARCH = {
    "win-x64": "amd64",
    "win-arm64": "arm64",
}

STANDALONE_TRIPLES = {
    "linux-x64": "x86_64-unknown-linux-gnu",
    "linux-arm64": "aarch64-unknown-linux-gnu",
    "osx-x64": "x86_64-apple-darwin",
    "osx-arm64": "aarch64-apple-darwin",
}

# 目标解释器在本机跑不起来时（交叉构建），让 uv 按这个平台解析轮子。
# 取值全部来自 `uv pip install --help` 的 possible values（本机 uv 0.12.22 核对）。
UV_PLATFORMS = {
    "win-x64": "x86_64-pc-windows-msvc",
    "win-arm64": "aarch64-pc-windows-msvc",
    "linux-x64": "x86_64-manylinux_2_28",
    "linux-arm64": "aarch64-manylinux_2_28",
    "osx-x64": "x86_64-apple-darwin",
    "osx-arm64": "aarch64-apple-darwin",
}

SUPPORTED_PLATFORMS = tuple(sorted({*EMBED_ARCH, *STANDALONE_TRIPLES}))

# 只认 install_only_stripped。triple 里含连字符，字符类必须带上 '-'（写在末尾当字面量）。
# 注意 -freethreaded- 变体是能被这个正则匹配上的（triple 会吃成 "...-freethreaded"），
# 它由 resolve_standalone_asset() 里的 triple 相等比较挡掉，不靠正则。
STANDALONE_ASSET_RE = re.compile(
    r"^cpython-(?P<minor>\d+\.\d+)\.\d+\+(?P<tag>\d+)"
    r"-(?P<triple>[A-Za-z0-9_.-]+)-install_only_stripped\.tar\.gz$"
)

STANDALONE_API = (
    "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
)
EMBED_URL = "https://www.python.org/ftp/python/{ver}/python-{ver}-embed-{arch}.zip"

# site-packages 的相对路径只在这里定义一次，
# patch_windows_pth() 写进 ._pth 的字符串和 site_packages() 返回的路径都由它派生。
WINDOWS_SITE_PACKAGES = ("Lib", "site-packages")
UNIX_SITE_PACKAGES = ("lib", f"python{PYTHON_MINOR}", "site-packages")

# 安装完成后必须存在的顶层目录名（大小写敏感：Linux 上 MaaAgentBinary 不是 maaagentbinary）。
AGENT_MODULES = ("maa", "numpy", "strenum", "MaaAgentBinary")

USER_AGENT = "MaaAshArms/setup_python_runtime"

# IncompleteRead / RemoteDisconnected 都在 http.client.HTTPException 下；
# HTTPError 是 URLError 的子类。这几类都必须显式捕获，否则一次抖动就整条流程失败。
_RETRYABLE = (
    urllib.error.URLError,
    http.client.HTTPException,
    TimeoutError,
    ConnectionError,
)


class SetupError(RuntimeError):
    """脚本自己发现的问题。main() 统一转成 exit(1) + 一句中文原因。"""


# ---------------------------------------------------------------------------
# 段 2：平台表自检 —— 回头校验段 1
# ---------------------------------------------------------------------------


def assert_platform_tables() -> None:
    """段 1 的三张表必须各自覆盖它负责的那组平台。

    - EMBED_ARCH：只有 Windows 两档用 python.org 的 embeddable 包
    - STANDALONE_TRIPLES：只有 macOS/Linux 用 python-build-standalone
    - UV_PLATFORMS：六档都要能给出交叉安装标签

    不这么做：跑 win-arm64 时可能在 STANDALONE_TRIPLES 里 KeyError，
    或者某个分支悄悄用了别的平台的标签。宁可启动第一秒就报错。
    """
    windows = {p for p in SUPPORTED_PLATFORMS if p.startswith("win-")}
    others = set(SUPPORTED_PLATFORMS) - windows
    for table_name, table, expected in (
        ("EMBED_ARCH", set(EMBED_ARCH), windows),
        ("STANDALONE_TRIPLES", set(STANDALONE_TRIPLES), others),
        ("UV_PLATFORMS", set(UV_PLATFORMS), set(SUPPORTED_PLATFORMS)),
    ):
        if table != expected:
            raise SetupError(
                f"段 1 的 {table_name} 与它负责的平台不一致："
                f"多出 {sorted(table - expected)}，缺少 {sorted(expected - table)}"
            )


# ---------------------------------------------------------------------------
# 段 3：网络与完整性校验
# ---------------------------------------------------------------------------


def _stream_to(response, handle, already: int) -> int:
    """按块读，避免 read() 一次性读整个 body（大文件下更容易被中途掐断）。"""
    total = already
    while True:
        chunk = response.read(1 << 20)
        if not chunk:
            return total
        handle.write(chunk)
        total += len(chunk)


def http_get(
    url: str, *, accept: str | None = None, token: str | None = None, attempts: int = 4
) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers)
            ) as response:
                buffer = io.BytesIO()
                _stream_to(response, buffer, 0)
                return buffer.getvalue()
        except urllib.error.HTTPError as exc:
            hint = ""
            if exc.code == 403 and "api.github.com" in url:
                hint = "（GitHub API 未认证时限额 60 次/小时，CI 里要带 GITHUB_TOKEN）"
            # 4xx（除 408/429）重试没意义
            if exc.code < 500 and exc.code not in (408, 429):
                raise SetupError(f"下载失败 {url}：HTTP {exc.code}{hint}") from exc
            last = exc
        except _RETRYABLE as exc:
            last = exc
        if attempt < attempts:
            print(f"[setup] 第 {attempt}/{attempts} 次请求失败，重试：{last!r}")
            time.sleep(2 * attempt)
    raise SetupError(f"下载失败（重试 {attempts} 次）：{url}：{last!r}")


def http_download(url: str, target: Path, *, attempts: int = 4) -> Path:
    """流式下载 + 断点续传 + 重试。

    大文件（python-build-standalone 约 35 MB）在国内直连 GitHub 经常被中途掐断，
    表现为 Content-Length 远大于实收字节数。这里把已收到的部分留在 .part 里，
    下次尝试带 Range 续传；服务器不支持 Range（非 206）就从头再来。
    完整性最终由 verify_sha256() 兜底（standalone 资产有官方摘要）。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    last: Exception | None = None

    for attempt in range(1, attempts + 1):
        written = partial.stat().st_size if partial.exists() else 0
        headers = {"User-Agent": USER_AGENT}
        if written:
            headers["Range"] = f"bytes={written}-"
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers)
            ) as response:
                if written and response.status != 206:
                    print("[setup] 服务器不支持续传（非 206），从头下载")
                    written = 0
                    partial.unlink(missing_ok=True)
                mode = "ab" if written else "wb"
                with partial.open(mode) as handle:
                    written = _stream_to(response, handle, written)
            partial.replace(target)
            print(
                f"[setup] 下载完成 {target.name}（{written / 1048576:.1f} MB，第 {attempt} 次尝试）"
            )
            return target
        except _RETRYABLE as exc:
            last = exc
            got = partial.stat().st_size if partial.exists() else 0
            print(
                f"[setup] 第 {attempt}/{attempts} 次下载中断（实收 {got / 1048576:.1f} MB）：{exc!r}"
            )
            if attempt < attempts:
                time.sleep(2 * attempt)

    raise SetupError(
        f"下载失败（重试 {attempts} 次）：{url}：{last!r}"
        "（国内直连 GitHub 大文件常被中断，可设 HTTPS_PROXY 走本机代理后重试）"
    )


def verify_sha256(path: Path, expected: str) -> None:
    """下载的是二进制发行版，必须能证明没被截断/替换。

    只有 python-build-standalone 提供了机器可读的摘要（GitHub 资产的 digest 字段）；
    python.org 的 embed zip 只在发布页给 MD5，没有稳定接口，所以校验只做在能做的分支。
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != expected.lower():
        raise SetupError(f"{path.name} sha256 不匹配：期望 {expected}，实际 {actual}")
    print(f"[setup] sha256 校验通过 {path.name}")


# ---------------------------------------------------------------------------
# 段 4：把 requirements.txt 钉的 maafw 和 install.yml 的 MAAFW_VERSION 对上
# ---------------------------------------------------------------------------


def assert_maafw_pinned(req_path: Path, maafw_version: str) -> None:
    """agent 侧（pip 包）和 client 侧（GUI 自带的原生框架）必须同版本。

    不同版本 → 协议号不一致 → 用户侧握手失败（Please update AgentServer / AgentClient）。
    这个对齐点原本只靠人记，放在这里让 CI 直接拦。
    """
    expected = maafw_version.strip().lstrip("vV")
    text = req_path.read_text(encoding="utf-8")
    found = re.findall(
        r"^\s*maafw\s*==\s*([0-9][^\s;#]*)", text, flags=re.IGNORECASE | re.MULTILINE
    )
    if not found:
        raise SetupError(f"{req_path} 里找不到 maafw== 的版本钉；先按 2.1 生成锁定清单")
    for version in found:
        if version != expected:
            raise SetupError(
                f"版本不一致：{req_path} 里是 maafw=={version}，MAAFW_VERSION 是 {maafw_version}"
            )
    print(f"[setup] maafw=={expected} 与 MAAFW_VERSION 一致")


# ---------------------------------------------------------------------------
# 段 5：Windows 分支（python.org embeddable）
# ---------------------------------------------------------------------------


def prepare_windows(platform: str, dest: Path, workdir: Path) -> None:
    arch = EMBED_ARCH[platform]
    filename = f"python-{PYTHON_EMBED_VERSION}-embed-{arch}.zip"
    archive = http_download(
        EMBED_URL.format(ver=PYTHON_EMBED_VERSION, arch=arch), workdir / filename
    )
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(dest)
    patch_windows_pth(dest)


def patch_windows_pth(root: Path) -> None:
    """修 python*._pth。

    不改的后果：embeddable 版默认把 `import site` 注释掉、也不把 Lib\\site-packages
    放进搜索路径，装进去的 MaaFw 会 import 不到（现象是"包里明明有依赖却 ModuleNotFoundError"）。
    """
    candidates = sorted(
        path
        for path in root.iterdir()
        if re.fullmatch(r"python\d*\._pth", path.name, re.IGNORECASE)
    )
    if len(candidates) != 1:
        raise SetupError(
            f"{root} 下的 python*._pth 不是恰好一个：{[p.name for p in candidates]}"
        )
    pth = candidates[0]

    lines = [line.rstrip("\r") for line in pth.read_text(encoding="utf-8").split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()

    updated = [
        "import site" if line.strip() in ("#import site", "# import site") else line
        for line in lines
    ]
    if not any(line.strip() == "import site" for line in updated):
        updated.append("import site")
    # 路径项由 WINDOWS_SITE_PACKAGES 派生，保证和 site_packages() 指向同一个目录。
    for entry in (".", "Lib", "\\".join(WINDOWS_SITE_PACKAGES), "DLLs"):
        if not any(line.strip() == entry for line in updated):
            updated.append(entry)

    # CPython 解析 _pth 时会去掉行尾 \r，写 LF 即可。
    pth.write_text("\n".join(updated) + "\n", encoding="utf-8")
    print(f"[setup] 已改写 {pth.name}")


# ---------------------------------------------------------------------------
# 段 6：macOS / Linux 分支（python-build-standalone）
# ---------------------------------------------------------------------------


def resolve_standalone_asset(platform: str, token: str | None) -> dict[str, str]:
    payload = json.loads(
        http_get(STANDALONE_API, accept="application/vnd.github+json", token=token)
    )
    tag = str(payload.get("tag_name") or "")
    triple = STANDALONE_TRIPLES[platform]
    for asset in payload.get("assets") or []:
        name = str(asset.get("name") or "")
        match = STANDALONE_ASSET_RE.match(name)
        if not match:
            continue
        if (
            match["minor"] != PYTHON_MINOR
            or match["triple"] != triple
            or match["tag"] != tag
        ):
            continue
        url = str(asset.get("browser_download_url") or "")
        digest = str(asset.get("digest") or "")
        if not url or not digest.startswith("sha256:"):
            raise SetupError(f"资产缺少下载地址或 sha256 摘要：{name}")
        return {"name": name, "url": url, "sha256": digest.split(":", 1)[1]}
    available = sorted(
        str(asset.get("name") or "")
        for asset in payload.get("assets") or []
        if str(asset.get("name") or "").startswith(f"cpython-{PYTHON_MINOR}.")
    )
    raise SetupError(
        f"没找到 {platform}（{triple}）的 CPython {PYTHON_MINOR} install_only_stripped 资产，"
        f"release tag={tag}；资产命名规则可能变了。"
        f"该 tag 下 CPython {PYTHON_MINOR} 的资产共 {len(available)} 个，前几个：{available[:5]}"
    )


def prepare_standalone(
    platform: str, dest: Path, workdir: Path, token: str | None
) -> None:
    asset = resolve_standalone_asset(platform, token)
    archive = http_download(asset["url"], workdir / asset["name"])
    verify_sha256(archive, asset["sha256"])
    count = extract_tar_manual(archive, dest)
    print(f"[setup] 从 {asset['name']} 解出 {count} 个文件")
    ensure_bin_python3(dest)


def extract_tar_manual(archive: Path, dest: Path) -> int:
    """手工逐成员解压，不用 tarfile.extractall。

    两个原因：
    1. 包里 bin/python3 是符号链接，extractall 会跳过符号链接 → 解出来没有 python3 入口；
    2. 需要剥掉顶层目录（install_only 包的结构是 python/bin/python3.13）。
    顺带按成员权限 chmod，并跳过 .DS_Store / __MACOSX。
    """
    written = 0
    with tarfile.open(archive, "r:gz") as tar:
        members = [member for member in tar.getmembers() if member.isfile()]
        if not members:
            raise SetupError(f"{archive.name} 里没有普通文件，包结构可能变了")
        roots = {member.name.split("/", 1)[0] for member in members}
        strip_root = len(roots) == 1 and all("/" in member.name for member in members)

        for member in members:
            name = member.name.replace("\\", "/")
            if strip_root:
                name = name.split("/", 1)[1]
            for prefix in ("python/", "install/"):
                if name.startswith(prefix):
                    name = name[len(prefix) :]
            if not name or name.endswith("/"):
                continue
            lowered = name.lower()
            if (
                lowered == ".ds_store"
                or lowered.endswith("/.ds_store")
                or lowered.startswith("__macosx/")
            ):
                continue

            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                continue
            with source, target.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            os.chmod(target, member.mode & 0o777)
            written += 1
    return written


def ensure_bin_python3(dest: Path) -> None:
    """保证 bin/python3 存在。

    tar 里 bin/python3 是指向 python3.13 的符号链接（解压时符号链接被跳过），
    这里把真身直接改名过去，而不是复制：复制会多出约 30 MB，
    zip 打包时还会再存一份（zip 没有硬链接概念）。
    包内 child_exec 用的是 python/bin/python3，没有任何地方按 python3.13 调用，改名不丢功能。
    """
    exe = dest / "bin" / "python3"
    if exe.exists():
        os.chmod(exe, 0o755)
        return

    bin_dir = dest / "bin"
    if not bin_dir.is_dir():
        raise SetupError(f"{dest} 里没有 bin/，包结构可能变了")
    for name in ("python3.13", "python3.13t", "python", "python3"):
        candidate = bin_dir / name
        if candidate.is_file():
            size_mb = candidate.stat().st_size / 1048576
            candidate.rename(exe)
            os.chmod(exe, 0o755)
            print(f"[setup] bin/python3 ← {name} 改名（省 {size_mb:.1f} MB 副本）")
            return
    raise SetupError(
        f"{bin_dir} 里找不到可用的解释器：{sorted(p.name for p in bin_dir.iterdir())}"
    )


# ---------------------------------------------------------------------------
# 段 7：路径、执行能力、装依赖
# ---------------------------------------------------------------------------


def python_executable(dest: Path, platform: str) -> Path:
    """解释器入口路径。全脚本只有这一处定义，段 5/6/8 都从这里取。"""
    return (
        dest / "python.exe" if platform.startswith("win-") else dest / "bin" / "python3"
    )


def site_packages(dest: Path, platform: str) -> Path:
    """依赖安装目标目录。Windows 分支与段 5 写进 ._pth 的字符串同源。"""
    parts = WINDOWS_SITE_PACKAGES if platform.startswith("win-") else UNIX_SITE_PACKAGES
    return dest.joinpath(*parts)


def can_execute(exe: Path) -> bool:
    """目标解释器能否在本机执行 —— 决定"真跑一遍"还是"按目录核对"。"""
    try:
        proc = subprocess.run(
            [str(exe), "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return proc.returncode == 0


def run(cmd: list[str]) -> None:
    print("[setup] $", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError as exc:
        raise SetupError(
            f"找不到命令 {cmd[0]}（CI 里需要 astral-sh/setup-uv）"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise SetupError(f"命令失败（exit {exc.returncode}）：{' '.join(cmd)}") from exc


def install_requirements(exe: Path, dest: Path, platform: str, req_path: Path) -> None:
    """把 agent 依赖装进这份解释器。

    - 能执行：uv 直接装进解释器自己的环境（embeddable 没有 pip，这是必须用 uv 的原因之一）。
    - 不能执行（交叉）：装成 wheel 到 site-packages，只允许预编译轮子；
      这时没有解释器可以校验，所以段 8 的自检会退化成"按目录核对"。
    注意这里只负责装，不负责验；自检在 self_check()，且必须在装完之后跑。
    """
    if can_execute(exe):
        run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(exe),
                "--system",
                "-r",
                str(req_path),
            ]
        )
        return

    target = site_packages(dest, platform)
    target.mkdir(parents=True, exist_ok=True)
    run(
        [
            "uv",
            "pip",
            "install",
            "--target",
            str(target),
            "--python-version",
            PYTHON_MINOR,
            "--python-platform",
            UV_PLATFORMS[platform],
            "--only-binary",
            ":all:",
            "-r",
            str(req_path),
        ]
    )


# ---------------------------------------------------------------------------
# 段 8：自检 + 入口
# ---------------------------------------------------------------------------


def self_check(exe: Path, dest: Path, platform: str) -> None:
    """产出验收。少了这步，问题会推迟到用户机器上才暴露成 Agent failed to start。"""
    packages = site_packages(dest, platform)
    missing = [name for name in AGENT_MODULES if not (packages / name).exists()]
    if missing:
        raise SetupError(f"{packages} 里缺少：{missing}")
    if can_execute(exe):
        run(
            [
                str(exe),
                "-B",
                "-c",
                "import maa, numpy, strenum; print('agent imports ok')",
            ]
        )
        print("[setup] 自检通过：目录齐全 + 实际 import 成功")
    else:
        print("[setup] 自检通过：目录齐全（目标解释器本机不可执行，未做 import 验证）")


def guard_dest(dest: Path) -> None:
    """第一步就是清空 --dest，这里挡住"把仓库或它的上层当 --dest"的误传。"""
    resolved = dest.resolve()
    cwd = Path.cwd().resolve()
    if resolved == cwd or cwd.is_relative_to(resolved):
        raise SetupError(f"--dest 会覆盖当前工作目录或其上层目录：{resolved}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="准备发布包用的 Python 运行时（解释器 + agent 依赖）"
    )
    parser.add_argument("--platform", required=True, choices=SUPPORTED_PLATFORMS)
    parser.add_argument(
        "--dest",
        required=True,
        type=Path,
        help="输出目录，将来整目录拷成发布包的 python/",
    )
    parser.add_argument("--requirements", type=Path, default=Path("requirements.txt"))
    parser.add_argument(
        "--maafw-version",
        default=None,
        help="形如 v5.12.2；给了就断言 requirements.txt 里的 maafw 与它一致",
    )
    args = parser.parse_args(argv)

    assert_platform_tables()
    if not args.requirements.is_file():
        raise SetupError(f"找不到依赖清单：{args.requirements}")
    if args.maafw_version:
        assert_maafw_pinned(args.requirements, args.maafw_version)
    guard_dest(args.dest)

    print(
        f"[setup] platform={args.platform} python={PYTHON_EMBED_VERSION} dest={args.dest}"
    )
    if args.dest.exists():
        shutil.rmtree(args.dest)
    args.dest.mkdir(parents=True)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    with tempfile.TemporaryDirectory(prefix="setup-python-runtime-") as tmp:
        workdir = Path(tmp)
        if args.platform.startswith("win-"):
            prepare_windows(args.platform, args.dest, workdir)
        else:
            prepare_standalone(args.platform, args.dest, workdir, token)
        install_requirements(
            python_executable(args.dest, args.platform),
            args.dest,
            args.platform,
            args.requirements,
        )

    self_check(python_executable(args.dest, args.platform), args.dest, args.platform)
    print(f"[setup] 完成：{args.dest}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SetupError as exc:
        print(f"[setup] 失败：{exc}", file=sys.stderr)
        sys.exit(1)
