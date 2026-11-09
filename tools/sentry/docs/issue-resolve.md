# `issue-resolve`：按提交正文关闭 Sentry issue（`pre-push` 钩子）

```bash
python -m tools.sentry.cli issue-resolve --install          # 写 .git/hooks/pre-push（list-only）
python -m tools.sentry.cli issue-resolve --install --apply  # 同上，但真的写 Sentry
python -m tools.sentry.cli issue-resolve --check            # 配置 / hook 模式 / token 来源
python -m tools.sentry.cli issue-resolve --uninstall
```

## 它读什么、写什么

**tag 从哪来**，按优先级：

1. `--tag NAME`（可重复）：直接处理这个本地 tag，不读 stdin；
2. `pre-push` 的 stdin：每行 `<local ref> <local oid> <remote ref> <remote oid>`，只处理
   **新建**的 `refs/tags/v*`（remote oid 全零、local oid 非零）；
3. stdin **一个 ref 行都没有**（`pre-push` 不会这样，只会是人手动跑）→ 处理**最新的本地
   `v*` tag**，并在提示里写明按哪个 tag 处理。

第 2 条用 **local oid** 算提交范围，不是 tag 名：annotated tag 的 oid 是 tag object，
只有 stdin 给的那一份是 git 明确要推的对象。第 3 条只在"完全没有输入"时触发——stdin 里
**有行但没有新建 tag** 时仍是跳过（推分支、更新已有 tag 都不该把本地最新 tag 的 issue 再关一遍）。

提交范围与 issue 提取：

1. `git describe --tags --match "v*" --abbrev=0 <oid>^{commit}^` 找上一个 `v*` tag；
   找不到回退 `git tag --sort=-creatordate --list v*` 列表里它的下一项；都没有就跳过这个 tag
   （不扫全历史）。
2. `git log <上一个 tag>..<oid> --format=%B`，只认**行首**的 `Fixes` / `Closes` / `Resolves`
   后面跟的 `PYTHON-<ID>`。正文别处提到的编号不算（否则改一句注释、提一下问题号就会关 issue）。
3. 两步 REST：`GET /api/0/organizations/<org>/shortids/<SHORT-ID>/` 取 `groupId`，
   再 `PUT /api/0/issues/<groupId>/` 带 `{"status": "resolved"}`。直接把 short ID 塞
   `/api/0/issues/<ID>/` 会 404。只改 `status`，不加备注、不动事件、不删 issue。

输出一律走 stderr：`pre-push` 的 stdout 会被 `git-send-pack` 转发到对端，本机未必看得到。

## 为什么是本地钩子而不是 CI

CI 侧（`.github/workflows/install.yml` 的 tag 触发）要改 Sentry 状态，必须把 token 放进仓库
Secrets；本地 `pre-push` 让凭据完全留在本机，零 Secret。凭据来源按顺序：
`SENTRY_AUTH_TOKEN` 环境变量 → `SENTRY_CONFIG_DIR/cli.db`（或默认的 `~/.config/sentry/cli.db`）
的 `auth` 表 → 已装的 `sentry auth token`。org 取 `--org` / `SENTRY_ORG` /
[`config.json`](../config.json) 的 `target`。

代价（都是钩子的固有边界，不是本工具的 bug）：

- 只在你**本机手动 push** 时生效；CI、网页、别人推的 tag 都不触发；
- `git push --no-verify` 绕过它；
- `pre-push` 在**推送之前**执行——推送失败时脚本已经跑过了。所以写操作必须幂等
  （重复 resolve 仍返回 200），"推送是否成功"当作未知；
- 只在本机装了 hook 的克隆里生效，`git clone` 不会带走 `.git/hooks`。

## 手动跑（不经过 push）

`python -m tools.sentry.cli issue-resolve` 直接跑时没有 `pre-push` 输入，会按**最新的本地
`v*` tag** 处理（`git tag --sort=-creatordate --list v*` 的第一个），先出清单；确认后
`--apply` 才写远端。看到 `没有新建的 v* tag，跳过` 说明 stdin 里有行但不是新建 tag（例如只推
分支），或者本地确实没有 `v*` tag。

要处理别的 tag（补跑、重放某个版本）用 `--tag NAME`，可重复：

```bash
python -m tools.sentry.cli issue-resolve --tag v1.0.0-beta.70
python -m tools.sentry.cli issue-resolve --tag v1.0.0-beta.70 --tag v1.0.0-beta.71 --apply
```

`--tag` 名字不在本地时只告警并跳过（先 `git fetch --tags`）。

## 默认只列清单

`--install` 默认生成 **list-only** 的 hook：只打印"新 tag / 扫到哪些 issue / 待关闭"，不写远端。
确认无误后再 `--install --apply`。两种 hook 都**恒 exit 0**（含内部异常），任何失败只告警、
不阻断 push——`pre-push` 非零退出会拦下整个 push，那不是一个"关 issue"的脚本该做的事。

## 验证与排查

- `--check`：打印仓库、`config.json` 的 target、org、hook 路径与模式、token 来源（只显示有没有，
  不打印 token）。
- `--ref LINE`（可重复）/ `--stdin-file`：不用真 push 就能模拟 hook 输入，例如
  `--ref "refs/tags/v1.0.0-beta.70 <oid> refs/tags/v1.0.0-beta.70 000…0"`。
- `--tag NAME`：不模拟 stdin，直接指定要处理的本地 tag（见上「手动跑」）。和手动直跑一样默认只列清单。
- `--only --issue <SHORT-ID>`：只用给定的 ID、**不扫提交范围**。验证 REST 分支时用它——`--issue`
  是**追加**语义，不带 `--only` 会把范围里扫到的真实 issue 一起关掉（踩过：验证 404 分支时误关了
  两条，见 `.agents/procedures/_inbox.md`）。
- 纯函数自检在 `python -m tools.sentry.cli selftest`（`lib/selftests/issue_resolve.py`）。

## 换位置或升级之后

hook 里写的是 `cd <仓库根>` + `python -m tools.sentry.cli issue-resolve`，所以脚本搬目录**不用**
改 hook；但换了 Python 解释器、或从旧版（直接指向脚本文件）升上来时要重跑 `--install`
（`--install --apply` 保持 apply 模式）。`--uninstall` 只删除带
`# managed-by: sentry_issue_resolve` 标记的 hook，别人的 hook 不会被覆盖或删掉（除非 `--force`）。
