from pathlib import Path

import argparse
import io
import shutil
import sys

# Windows runner 的控制台编码是 cp1252，Python 默认拿它当 stdout/stderr 编码，print 中文就
# UnicodeEncodeError（本机 cp936 不复现，CI 的 windows 档会撞）。只调 errors 不动 encoding：
# 本机保持原编码、不乱码；编码装不下时退化成替代字符而不是崩。同一个进程里 configure.py
# 的 print 也受这一处影响。CI 侧另有 job 级 PYTHONUTF8=1。
for _stream in (sys.stdout, sys.stderr):
    # reconfigure() 定义在 io.TextIOWrapper 上，而 sys.stdout 的静态类型是 typing.TextIO，
    # 直接调用会被 Pylance 报 reportAttributeAccessIssue；isinstance 收窄后静态与运行时都成立。
    if isinstance(_stream, io.TextIOWrapper):
        try:
            _stream.reconfigure(errors="replace")
        except (OSError, ValueError):
            pass

try:
    import jsonc
except ModuleNotFoundError as e:
    raise ImportError(
        "Missing dependency 'json-with-comments' (imported as 'jsonc').\n"
        f"Install it with:\n  {sys.executable} -m pip install json-with-comments\n"
        "Or add it to your project's requirements."
    ) from e

from configure import configure_ocr_model

working_dir = Path(__file__).parent.parent.resolve()
install_path = working_dir / Path("install")

# ---------------------------------------------------------------------------
# 段 1：命令行
#
# 参数在模块级解析，下面几个函数继续直接读全局变量 —— 与原文件的结构保持一致，
# 少一层传递。代价是 import 本模块就会解析 argv，对一次性构建脚本无所谓。
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="把 assets/ + agent/ + deps/（可选 MFA、可选 Python 运行时）组装成 install/ 发布包",
    )
    parser.add_argument(
        "version", help="版本号，会写进包内 interface.json，例如 v1.0.0"
    )
    parser.add_argument(
        "os_name", choices=["win", "macos", "linux", "android"], help="目标平台"
    )
    parser.add_argument("arch", choices=["aarch64", "x86_64"], help="目标架构")
    parser.add_argument(
        "--mfa-dir",
        type=Path,
        default=None,
        help="MFAAvalonia 解压目录；给了就整体铺进包并把 GUI 入口改名成 MaaAshArms",
    )
    parser.add_argument(
        "--python-runtime",
        type=Path,
        default=None,
        help="tools/ci/setup_python_runtime.py 的产出目录；给了就拷成包内 python/ 并改写 agent.child_exec",
    )
    return parser.parse_args()


ARGS = parse_args()
version = ARGS.version
os_name = ARGS.os_name
arch = ARGS.arch


# ---------------------------------------------------------------------------
# 段 2：MFAAvalonia 本体（原来是工作流里的 rsync + mv）
# ---------------------------------------------------------------------------


def install_mfa(mfa_dir: Path | None) -> None:
    """把 GUI 的文件铺到包的根。

    搬进 Python 的原因：换原生 runner 后 Windows/macOS 上的 `shell: bash` 是 Git Bash，
    `rsync` 不一定存在（原来 install.yml 里那一行会直接失败，包就没有 GUI）。
    `ignore_patterns("runtimes")` 等价于原来那句 `rm -rf MFA/runtimes`：GUI 自带的那份原生库不要，
    随后 install_deps() 会把 deps/bin 那份写进 install/runtimes/<platform-tag>/native。
    """
    if mfa_dir is None or not mfa_dir.is_dir():
        print(f"MFA directory not found ({mfa_dir}), skipping copy.")
        return

    shutil.copytree(
        mfa_dir,
        install_path,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("runtimes"),
    )

    suffix = ".exe" if os_name == "win" else ""
    source = install_path / f"MFAAvalonia{suffix}"
    target = install_path / f"MaaAshArms{suffix}"
    if not source.exists():
        print(f"{source.name} not found, skipping rename.")
        return
    target.unlink(missing_ok=True)
    source.rename(target)
    print(f"GUI entry renamed: {source.name} -> {target.name}")


# ---------------------------------------------------------------------------
# 段 3：MaaFramework 原生库（逻辑不变，只保留原样）
# ---------------------------------------------------------------------------


def get_dotnet_platform_tag():
    """自动检测当前平台并返回对应的dotnet平台标签"""
    if os_name == "win" and arch == "x86_64":
        platform_tag = "win-x64"
    elif os_name == "win" and arch == "aarch64":
        platform_tag = "win-arm64"
    elif os_name == "macos" and arch == "x86_64":
        platform_tag = "osx-x64"
    elif os_name == "macos" and arch == "aarch64":
        platform_tag = "osx-arm64"
    elif os_name == "linux" and arch == "x86_64":
        platform_tag = "linux-x64"
    elif os_name == "linux" and arch == "aarch64":
        platform_tag = "linux-arm64"
    else:
        print("Unsupported OS or architecture.")
        print("available parameters:")
        print("version: e.g., v1.0.0")
        print("os: [win, macos, linux, android]")
        print("arch: [aarch64, x86_64]")
        sys.exit(1)

    return platform_tag


def install_deps():
    if not (working_dir / "deps" / "bin").exists():
        print('Please download the MaaFramework to "deps" first.')
        print('请先下载 MaaFramework 到 "deps"。')
        sys.exit(1)

    if os_name == "android":
        shutil.copytree(
            working_dir / "deps" / "bin",
            install_path,
            dirs_exist_ok=True,
        )
        shutil.copytree(
            working_dir / "deps" / "share" / "MaaAgentBinary",
            install_path / "MaaAgentBinary",
            dirs_exist_ok=True,
        )
    else:
        shutil.copytree(
            working_dir / "deps" / "bin",
            install_path / "runtimes" / get_dotnet_platform_tag() / "native",
            ignore=shutil.ignore_patterns(
                "*MaaDbgControlUnit*",
                "*MaaThriftControlUnit*",
                "*MaaRpc*",
                "*MaaHttp*",
                "plugins",
                "*.node",
                "*MaaPiCli*",
            ),
            dirs_exist_ok=True,
        )
        shutil.copytree(
            working_dir / "deps" / "share" / "MaaAgentBinary",
            install_path / "libs" / "MaaAgentBinary",
            dirs_exist_ok=True,
        )
        shutil.copytree(
            working_dir / "deps" / "bin" / "plugins",
            install_path / "plugins" / get_dotnet_platform_tag(),
            dirs_exist_ok=True,
        )


# ---------------------------------------------------------------------------
# 段 4：资源 + interface.json（新增 agent 改写）
# ---------------------------------------------------------------------------


def rewrite_agent(interface: dict, python_runtime: Path | None) -> None:
    """把包内 interface.json 的 agent 指向包内解释器。

    开发态（assets/interface.json）保持 "child_exec": "python"：包内路径在开发机上不存在，
    改了本地就没法用系统 Python 跑 agent（VSCode / maa-tools 调试都受影响）。
    MFA 按 AppPaths.DataRoot 解析 child_exec（AgentHelper.cs:219，`ReplacePlaceholder(..., DataRoot, true)`），
    DataRoot 就是安装根，所以 "python/python.exe" 这种相对路径能落到包内。
    `-u` 关掉 stdout 缓冲，agent 的 print 才会实时出现在 MFA 面板里。
    """
    if python_runtime is None:
        return

    agents = interface.get("agent")
    if agents is None:
        print("interface.json has no 'agent' field, skipping rewrite.")
        return

    child_exec = "python/python.exe" if os_name == "win" else "python/bin/python3"
    items = (
        agents if isinstance(agents, list) else [agents]
    )  # schema 里 agent 是 oneOf 对象/数组
    for agent in items:
        if not isinstance(agent, dict):
            continue
        agent["child_exec"] = child_exec
        args = [item for item in (agent.get("child_args") or []) if item != "-u"]
        agent["child_args"] = ["-u", *args]
        print(f"agent: child_exec={child_exec}, child_args={agent['child_args']}")


def install_resource(python_runtime: Path | None):
    configure_ocr_model()

    shutil.copytree(
        working_dir / "assets" / "resource",
        install_path / "resource",
        dirs_exist_ok=True,
    )
    shutil.copy2(
        working_dir / "assets" / "interface.json",
        install_path,
    )

    with open(install_path / "interface.json", "r", encoding="utf-8") as f:
        interface = jsonc.load(f)

    interface["version"] = version
    rewrite_agent(interface, python_runtime)

    with open(install_path / "interface.json", "w", encoding="utf-8") as f:
        jsonc.dump(interface, f, ensure_ascii=False, indent=4)


# ---------------------------------------------------------------------------
# 段 5：杂项与 agent 拷贝
# ---------------------------------------------------------------------------


def install_chores():
    shutil.copy2(
        working_dir / "README.md",
        install_path,
    )
    shutil.copy2(
        working_dir / "LICENSE",
        install_path,
    )


def install_agent():
    """拷 agent 源码，过滤构建期垃圾。

    `__pycache__` / `*.pyc` 与解释器版本绑定，开发机或 CI 上跑过一次 agent 就会留下，
    打进包既没用又可能让用户 import 到不匹配的字节码。
    """
    shutil.copytree(
        working_dir / "agent",
        install_path / "agent",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )


# ---------------------------------------------------------------------------
# 段 6：Python 运行时
# ---------------------------------------------------------------------------


def strip_agent_native_runtime(installed: Path) -> None:
    """删掉 agent 侧重复的 MaaFramework 原生库（win-x64 实测 59.1 MiB）。

    agent/maafw_paths.py 会在 import maa 之前把 MAAFW_BINARY_PATH 指到客户端那份
    （install/runtimes/<platform-tag>/native），所以 wheel 自带的 site-packages/maa/bin
    是纯冗余。一个都没删到就报错——否则 MaaFw 改了 wheel 布局后会静默多打一份。
    """
    removed: list[Path] = []
    for bin_dir in (installed / "python").rglob("bin"):
        if (
            bin_dir.is_dir()
            and bin_dir.parent.name == "maa"
            and bin_dir.parent.parent.name == "site-packages"
        ):
            shutil.rmtree(bin_dir, ignore_errors=True)
            removed.append(bin_dir)
    if not removed:
        sys.exit("install/python 下没找到 site-packages/maa/bin：MaaFw 的 wheel 布局可能变了")


def ensure_agent_native_plugins_dir(installed: Path) -> None:
    """给 agent 用的那份原生库补一个空 plugins/。

    MaaFramework 的 PluginMgr 把"插件目录不存在"当加载失败，每次启动打 4 行 ERR；空目录则安静。
    本包的插件在 install/plugins/<tag>，不在 runtimes/<tag>/native 下，而 agent 现在从后者加载，
    所以补一个空目录（M9A 的 ensureClientNativePluginsDir 是同一个处理）。
    """
    (installed / "runtimes" / get_dotnet_platform_tag() / "native" / "plugins").mkdir(
        parents=True, exist_ok=True
    )


def install_python_runtime(python_runtime: Path | None) -> None:
    """把便携解释器铺到包根 python/，并去掉里面重复的原生库。

    放包根而不是 agent/ 下：资源更新的全量删除只覆盖 DataRoot/resource/** 与 DataRoot/agent/**，
    放 agent/ 里每次资源更新都会被删掉，用户就又要自己装 Python。
    """
    if python_runtime is None:
        print("No --python-runtime given, skipping (agent 将依赖用户自装的 Python)。")
        return
    if not python_runtime.is_dir():
        sys.exit(f"--python-runtime 不存在：{python_runtime}")

    shutil.copytree(python_runtime, install_path / "python", dirs_exist_ok=True)
    strip_agent_native_runtime(install_path)
    ensure_agent_native_plugins_dir(install_path)
    print(f"Python runtime installed: {install_path / 'python'}（已剔除重复原生库）")


# ---------------------------------------------------------------------------
# 段 7：冒烟
# ---------------------------------------------------------------------------


def smoke_check(python_runtime: Path | None) -> None:
    """打包最后一环的产出验收。

    在这里失败，好过让用户在 MFA 里看到 `Agent 'python' failed to start`。
    只检查 `agent/` 下没有 __pycache__，不检查 `python/`：解释器一 import 就会生成字节码，
    那是正常的（setup_python_runtime.py 的自检用 -B 避免它，但用户运行后必然会有）。
    """
    problems: list[str] = []

    if not (install_path / "agent" / "main.py").is_file():
        problems.append("install/agent/main.py 缺失")
    for cache in (install_path / "agent").rglob("__pycache__"):
        problems.append(
            f"install/agent 下不该有构建缓存：{cache.relative_to(install_path)}"
        )

    if os_name != "android":
        native = install_path / "runtimes" / get_dotnet_platform_tag() / "native"
        if not native.is_dir():
            problems.append(
                "install/runtimes/<platform-tag>/native 缺失（原生框架没铺进去）"
            )
        elif not (native / "plugins").is_dir():
            problems.append(
                "agent 使用的原生库目录缺少 plugins/（MaaFramework 每次启动会打 ERR）"
            )
        if not (install_path / "libs" / "MaaAgentBinary").is_dir():
            problems.append("install/libs/MaaAgentBinary 缺失")

    with open(install_path / "interface.json", "r", encoding="utf-8") as f:
        interface = jsonc.load(f)

    if python_runtime is not None:
        exe_rel = "python.exe" if os_name == "win" else "bin/python3"
        if not (install_path / "python" / exe_rel).is_file():
            problems.append(f"install/python/{exe_rel} 缺失")

        leftover = [
            p
            for p in (install_path / "python").rglob("bin")
            if p.is_dir()
            and p.parent.name == "maa"
            and p.parent.parent.name == "site-packages"
        ]
        if leftover:
            problems.append(
                f"install/python 下仍有重复原生库：{leftover[0].relative_to(install_path)}"
            )

        expected = "python/python.exe" if os_name == "win" else "python/bin/python3"
        agents = interface.get("agent")
        items = agents if isinstance(agents, list) else [agents]
        actual = [item.get("child_exec") for item in items if isinstance(item, dict)]
        if not actual or any(value != expected for value in actual):
            problems.append(
                f"包内 interface.json 的 agent.child_exec 应为 {expected}，实际 {actual}"
            )

    if problems:
        for problem in problems:
            print(f"[smoke] {problem}")
        sys.exit(1)
    print("[smoke] OK")


# ---------------------------------------------------------------------------
# 段 8：入口
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    install_path.mkdir(parents=True, exist_ok=True)

    # 顺序有讲究：先铺 GUI（且不带它自带的 runtimes），再让 install_deps() 往
    # runtimes/<platform-tag>/native 写框架；反过来会被 copytree 覆盖。
    install_mfa(ARGS.mfa_dir)
    install_deps()
    install_resource(ARGS.python_runtime)
    install_chores()
    install_agent()
    install_python_runtime(ARGS.python_runtime)
    smoke_check(ARGS.python_runtime)

    print(f"Install to {install_path} successfully.")
