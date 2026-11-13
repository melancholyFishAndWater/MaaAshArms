# 如何开发

在开始开发前请先阅读 MaaFramework 开发文档的 [快速开始](https://maafw.com/docs/1.1-QuickStarted) 章节，以便你对 MaaFramework 有一个基本的了解。

~~同时，我们还提供了一个 [🎞️ 视频教程](https://www.bilibili.com/video/BV1yr421E7MW) 以供参考。~~ 视频中使用的版本较老，一切问题须以最新版文档为准。

## 开发前提

使用本教程进行开发则默认你遵守 MaaFramework 衍生项目的相关开发规范以及共识，所有的讨论也将基于以下前提。

0. 拥有一个 GitHub 账号并且已经登录  
1. 使用基于 git 作为版本控制工具  
  如果你还不会用，可以先在 [菜鸟教程](https://www.runoob.com/git/git-tutorial.html) 进行学习。  
2. 使用 GitHub 托管代码并使用相关 [CI/CD 工作流](https://docs.github.com/zh/actions)。
  项目中附带了一些基于 [GitHub Actions](https://docs.github.com/zh/actions) 的 CI/CD 工作流配置，你可以通过他们来自动进行测试以及将项目打包和发布。  
3. 了解本框架中一些常见的术语  
  MaaFramework 手册中的 [术语解释](https://maafw.com/docs/1.2-ExplanationOfTerms) 章节介绍了一些基本的专有术语。  

## 开发步骤

0. 使用 [本项目主页](https://github.com/MaaXYZ/MaaPracticeBoilerplate) 右上角 `Use this template` - `Create a new repository` 来基于本模板创建您自己的项目。  
   _（如果你找不到这个按钮，说明你没有登录 GitHub 账号）_

1. 克隆你的项目（地址请修改为您基于本模板创建的新项目地址）。

   ```bash
   git clone https://github.com/<你的用户名>/<你的项目名称>.git
   ```

2. 下载 OCR（文字识别）资源文件 [ppocr_v6.zip](https://download.maafw.xyz/MaaCommonAssets/OCR/ppocr_v6/ppocr_v6-small.zip) 解压到 `assets/resource/model/ocr/` 目录下，确保路径如下：

   ```tree
   assets/resource/model/ocr/
   ├── det.onnx
   ├── keys.txt
   └── rec.onnx
   ```

   > [!WARNING]
   > 请注意，您不需要将 OCR 资源文件上传到您的代码仓库中。`.gitignore` 已经忽略了 `assets/resource/model/ocr/` 目录，且 GitHub workflow 在发布版本时会自动配置这些资源文件。

   _如果希望使用其他版本的模型，可以参考 [这个说明](https://github.com/MaaXYZ/MaaCommonAssets/tree/main/OCR)。_

3. 进行开发工作。请参考 [MaaFramework 相关文档](https://maafw.com/docs/1.1-QuickStarted)，并按您的业务需求修改 `assets` 目录下的 `resource` 资源文件以及 `interface.json` 文件，然后使用 [开发工具](https://maafw.com/docs/1.1-QuickStarted#%E8%B0%83%E8%AF%95) 进行调试。

   通常来说，您**不需要**为您的项目单独开发一套 UI，本模板附带了自动配置 _通用 UI_ 的持续集成（CI），使用方法请参考后续步骤。

4. 完成开发后，上传您的代码并发布版本。

   ```bash
   # 配置 git 信息（仅第一次需要，后续不用再配置）
   git config user.name "您的 GitHub 昵称"
   git config user.email "您的 GitHub 邮箱"

   # 提交修改
   git add .
   git commit -m "XX 新功能"
   git push origin HEAD -u
   ```

   如果您准备通过 PR 与他人协作，建议参考 [PR 规范](./pull_request_guidelines.md) 补充变更摘要、验证记录和必要的日志或截图。

5. 发布您的版本

   本模板附带 GitHub Actions 工作流的 [配置文件](/.github/workflows/install.yml)，CI 检测到 tag 会自动进行打包和发布。默认的配置文件会将 [MFAAvalonia](https://github.com/SweetSmellFox/MFAAvalonia) 与你的项目一同打包和发版。

   > [!NOTE]
   > 第一次操作前，需要**先**修改 GitHub 仓库设置 `Settings` - `Actions` - `General` - `Read and write permissions` - `Save`

   ```bash
   # 给最近的 commit 打上 v1.0.0 标签并推送
   git tag v1.0.0
   git push origin v1.0.0
   ```

   执行上述命令后，CI 会自动进行打包和发布，你可以在项目仓库的 `Actions` 页面中看到工作流的执行情况。如果一切顺利，运行结束后你可以在项目仓库的 `Releases` 页面中看到新发布的版本。更多有关 GitHub Actions 的内容请参考 [GitHub Actions 文档](https://docs.github.com/zh/actions)。

   _如果想要使用别的 [通用 UI](https://github.com/MaaXYZ/MaaFramework/#%E9%80%9A%E7%94%A8-ui)，请自行修改工作流的 [配置文件](/.github/workflows/install.yml)。_

## Python 开发环境

安装 uv 后，在仓库根执行：

```bash
uv sync --locked
uv run --locked python -m pytest -q tests
```

VS Code 的 Python 解释器选择仓库 `.venv`。开发态 agent 由 `assets/interface.json`
启动：`uv run --directory {PROJECT_DIR}/.. --locked python -u ./agent/main.py`。
这里 `{PROJECT_DIR}` 是 `assets/`，先切回仓库根，再使用项目锁文件与虚拟环境；
无需依赖 `assets/agent` 的目录联接。uv 必须能被 VS Code 的终端从 PATH 找到。
修改依赖后先更新并提交锁文件，普通任务启动不会自动改锁。

### 依赖来源与更新

- `pyproject.toml` 是唯一手写依赖声明：`project.dependencies` 为 agent 运行依赖，
  `build` 组为打包脚本依赖，`dev` 组包含 `build` 及测试、校验工具。
- `uv.lock` 固定全部依赖的解析版本，开发环境与 CI 都使用 `--locked` 检查声明和锁是否一致。
  不要用 `--frozen` 代替这项检查，它不会检查声明漂移。
- `requirements.txt` 是从锁文件生成的发布运行依赖清单，不要手工编辑；不包含任何依赖组。

在仓库根修改声明后执行（CI 固定 uv `0.12.22`，本地导出也使用这个版本）：

```bash
uv lock
uv sync --locked
uv export --locked --no-default-groups --no-emit-project --no-hashes --no-header --format requirements-txt --output-file requirements.txt
uv run --locked python -m pytest -q tests
```

`uv lock` 默认保留已有锁定版本，不要为了同步声明使用 `--upgrade`。
若确实要升级指定包，使用 `uv lock --upgrade-package <包名>`，重新导出并检查版本差异。
升级 MaaFramework 时还必须同步 `.github/workflows/install.yml` 的 `MAAFW_VERSION`；
发布运行时构建会检查它与导出清单中的 `maafw` 版本一致。
将声明、锁文件、导出清单作为同一批改动提交；连续执行导出命令应无额外差异。
`--no-header` 排除包含调用命令的生成头，避免输出路径改变造成无意义差异。

完整开发环境使用前面的 `uv sync --locked`；只运行打包脚本时可以使用：

```bash
uv run --locked --only-group build python tools/install.py --help
```

构建组不安装 agent 或测试依赖；该命令会将当前 `.venv` 同步成仅构建环境。
回到日常开发前重新执行 `uv sync --locked`。
CI 的检查 job 使用完整开发环境，发布矩阵仅使用构建组，
agent 运行依赖按导出清单另装进包内解释器，不复制开发 `.venv`。
两个工作流都会重新导出清单并用 `git diff --exit-code` 检查是否漏提交。

### Windows 启动检查

Windows agent 会在加载原生框架前检查临时目录创建能力。失败时报告进程完整性、
venv 和基础解释器路径；若解释器继承了 Low 标签，请先运行
`pwsh -File tools/dev/python_integrity.ps1` 只读检查。
显式修复和恢复步骤见 [开发环境标签工具](../../../tools/dev/README.md)。
正常开发不需要管理员权限；不要递归修改仓库或系统 Temp。

发布时打包脚本会完整覆盖开发命令：带运行时的包直接用包内 Python，
不带运行时的资源包使用宿主提供的系统 Python；两者都不需要 uv 或项目锁文件。

## 常见问题

请参考 [FAQ](./faq.md)。

## 更多操作

请参考 [个性化配置](./custom_configure.md)（可选）。

## 编码约定

新增或重命名 pipeline 节点、`custom_*` 注册名、任务参数键前，请先读 [节点命名与分类](./node-naming.md)；搭任务链、写页面入口与循环、决定 `[JumpBack]` 和 `max_hit` 前，请先读 [任务链结构](./pipeline-structure.md)。

改完跑一遍 `python tools/validation/cli.py validate-refs`：它按上面两份文档与 `interface.json` 的接线，检查跨文件的名字一致性（注册名 ↔ 节点名、节点引用、模板引用、`pipeline_override` 目标、preset/option、命名分类），只读、纯标准库。`--pipeline` / `--tasks` / `--agent` / `--image` / `--interface` 可只查某一域，`--ignore` 可忽略单条规则 id。
