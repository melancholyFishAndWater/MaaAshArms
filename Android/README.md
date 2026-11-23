# Android APK

本目录参照 [M9A](https://github.com/MAA1999/M9A/tree/main/Android) 接入 [MaaFwApp](https://github.com/Aliothmoon/MaaFwApp)。GUI 是固定提交的 Git 子模块，不在构建时追随 main；资源与 Python agent 来自本仓库。

## 使用条件

- Android 10 或以上，已安装灰烬战线 B 服（`com.asharmdolls.bilibili`）。
- Shizuku 或 root 授权。首版只开放 1280×720、240 dpi 后台虚拟屏幕；不要据此承诺前台长屏或其他渠道服兼容。
- 安装 APK 后先授权并连接服务，再保留“启动游戏”任务运行，确保游戏启动到虚拟屏幕。
- 旧的 `android-aarch64` / `android-x86_64` ZIP 不是 APK，不能直接安装。新的 tag 发布由 APK 替代这两档 ZIP。

APK 构建、签名通过不等于游戏任务已验收；需要在目标设备重复检查冷启动、已有前台游戏、再次运行、agent 连接及业务任务结果。当前仓库接入不能代替首次真机验收。

## 文件与流程

- `MaaFwApp/`：Android GUI 子模块；修改子模块指针前核对上游构建环境和 PI 行为。
- `profile.yaml`：应用标识、资源白名单、Python bundle 入口、日志导出与屏幕设置。
- `package.py stage`：组装 `staging/`。源资源在 `assets/`、agent 在根目录，不能只把其中之一作为 APK 资源根。
- `verify_apk.py`：检查实际 APK 内原生库、资源 ZIP、Python bundle 的架构与必要文件，不验证签名或设备行为。
- `../tools/android-packaging.mjs`：从 `requirements.txt` 解析 MaaFW 版本，配对 Android Python core，并处理额外原生 Python 依赖。

OCR 调用本仓库现有配置逻辑，从 `assets/MaaCommonAssets/OCR/ppocr_v6/small` 铺入，随后检查 det、rec、keys 均存在且非空。staging 不带框架 `.so`、运行日志或 Python 缓存；框架库由 MaaFwApp 单独装入 APK 的 jniLibs。

## 版本与 Python

Android agent 使用 [MaaAgentCoreAndroid](https://github.com/Aliothmoon/MaaAgentCoreAndroid) 的 CPython + maa 绑定内核。原生 MaaFramework 与内核绑定必须同版本。解析器默认要求与 `requirements.txt` 精确配对，找不到就失败，不自动退到旧框架，也不通过手动 latest 绕过配对。

内核已提供的 maafw、numpy、strenum 优先使用内核版本；桌面 requirements 的这些 pin 不会强制覆盖 Android 内核。maaagentbinary 是桌面组件，由外壳构建脚本丢弃。其他带原生扩展的 Python 包可能需要 Chaquopy Android wheel，不能假定桌面版本存在 Android 构建。

诊断用的 `--allow-fallback`（或 `ANDROID_PACKAGING_ALLOW_FALLBACK=1`）只允许更旧配对；正式 CI 不启用它。若自行降级，须重新评估 agent 所用 API，不能把内核与客户端版本匹配等同于业务兼容。

## CI 与签名

- `android.yml`：分支 push / PR 分别出 ARM64 与 x86_64 debug 工件；手动可以选 release，但不创建 GitHub Release。普通 ARM64 真机选 `android-apk-debug-arm64-v8a`，x86_64 MuMu 选 `android-apk-debug-x86_64`。模拟器能通过翻译层打开 ARM64 界面，不代表 x86_64 Shizuku 特权进程能加载 ARM64 原生库。
- `android-build.yml`：可复用 APK 流程。当前固定外壳采用 JDK 25、Android SDK（compile SDK 37）、NDK 29.0.13113456、CMake 3.22.1、Python 3.13；参考 M9A 使用 macOS runner 构建 APK，另用 Ubuntu 检查库模块资源。
- `install.yml`：正式 `v*` tag 同时调用桌面和 APK 构建，全部就绪后统一发到同一个 Release；R8 mapping 保留为 Actions 工件，不混进下载附件。

正式发布三个 APK：

| 文件中的架构段 | 包含内容 |
| --- | --- |
| `universal` | arm64-v8a + x86_64 |
| `arm64-v8a` | 普通 ARM64 手机 |
| `x86_64` | x86_64 Android 环境 |

仓库管理员须在 GitHub Actions Secrets 配置：

```text
KEYSTORE_BASE64
KEYSTORE_PASSWORD
KEY_ALIAS
KEY_PASSWORD
```

必须长期保留同一应用的签名密钥；换密钥会影响覆盖安装和更新。密钥不进 Git、不写入 PI。正式 tag 缺任何签名材料直接失败，不发布半套附件；手动 release 无密钥时只出带 `-unsigned` 标记的未签名工件，不能安装。debug 使用调试签名，不作为正式更新渠道。

## 本地构建

先准备与 CI 相匹配的 JDK、SDK、NDK、CMake，并配置 SDK 许可证。只安装旧版 Android Studio 或 SDK 34 不足以构建当前外壳。

在仓库根执行：

```powershell
git submodule update --init --recursive
uv sync --locked
node --test tests/android-packaging.test.mjs
uv run --locked python -m pytest -q tests/test_android_package.py tests/test_android_apk.py
node tools/android-packaging.mjs
```

最后一条需要联网及可用 GitHub API 配额；CI 使用 `GITHUB_TOKEN`。按输出的 `core_tag` / `maafw_tag` 执行（下面占位符须替换，不能原样运行）：

```powershell
python Android/MaaFwApp/scripts/setup_maa_framework.py --abi arm64-v8a --tag <maafw_tag>
python Android/MaaFwApp/scripts/build_agent_bundle.py --out Android/agent-dist --core-tag <core_tag> --abi arm64-v8a --requirements requirements.txt --extra-index-url https://chaquo.com/pypi-13.1/
uv run --locked python Android/package.py stage --version <版本号>
```

如解析器输出额外 `chaquopy_pins`，也应加到 bundle 命令中。`staging/` 必须为空；重复组装前由使用者确认并清理旧 staging，脚本不会自动删除已有数据。

在外壳的 `local.properties` 中配置本机路径（不入库）：

```properties
sdk.dir=<Android SDK 路径>
pi.profile=../profile.yaml
build.debugAbi=arm64-v8a
build.releaseAbi=arm64-v8a
build.versionName=<与 staging 一致的版本号>
```

```powershell
Android/MaaFwApp/gradlew.bat -p Android/MaaFwApp --no-daemon assembleDebug
python Android/verify_apk.py Android/MaaFwApp/app/build/outputs/apk/debug/app-debug.apk --abi arm64-v8a
```

多架构包须同时铺入所声明架构的框架库和 Python bundle，再设置对应 `build.releaseAbi`；不能仅修改 APK 的 ABI 过滤项。签名验证由 SDK 的 `apksigner verify --verbose` 执行。

## 许可

原有资源和 agent 仍遵循根 MIT LICENSE。MaaFwApp 以及参考 M9A 派生的 Android 版本解析脚本受 AGPL v3 约束，完整文本见 `LICENSE-AGPL-3.0`，来源和本地适配见脚本头。Android 分发和对应源码获取要求见 `NOTICE.md`。该接入不把上游代码重新许可为 MIT，也不改变桌面原有源码的许可声明。
