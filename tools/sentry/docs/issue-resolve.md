# `issue-resolve`：按提交正文关闭 Sentry issue（`pre-push` 钩子）

```bash
python -m tools.sentry.cli issue-resolve --install          # 写 .git/hooks/pre-push（list-only）
python -m tools.sentry.cli issue-resolve --install --apply  # 同上，但真的写 Sentry
python -m tools.sentry.cli issue-resolve --check            # 配置 / hook 模式 / token 来源
python -m tools.sentry.cli issue-resolve --uninstall
```

## 它读什么、写什么

**提交范围从哪来**，按优先级：

1. `--tag NAME`（可重复）：处理这个本地 tag 自己的提交（`<上一个 v* tag>..<NAME>`），不读 stdin；
2. `pre-push` 的 stdin：每行 `<local ref> <local oid> <remote ref> <remote oid>`，只处理
   **新建**的 `refs/tags/v*`（remote oid 全零、local oid 非零），范围同上；
3. stdin **一个 ref 行都没有**（`pre-push` 不会这样，只会是人手动跑）→ 算**最新 tag 之后**
   的提交（`<最新 tag>..HEAD`）：要发的版本还没打 tag，这才是"这次会关掉哪些 issue"；
   最新 tag 之后**没有**提交时（HEAD 就停在 tag 上）退回这个 tag 自己那段
   （`<次新 tag>..<最新 tag>`）。两段都没得算（最早的 tag + 没有新提交）就跳过，不扫全历史。

第 2 条用 **local oid** 算提交范围，不是 tag 名：annotated tag 的 oid 是 tag object，
只有 stdin 给的那一份是 git 明确要推的对象。第 3 条只在"完全没有输入"时触发——stdin 里
**有行但没有新建 tag** 时仍是跳过（推分支、更新已有 tag 都不该把本地最新 tag 的 issue 再关一遍）。

提交范围与 issue 提取：

1. `git describe --tags --match "v*" --abbrev=0 <oid>^{commit}^` 找上一个 `v*` tag；
   找不到回退 `git tag --sort=-creatordate --list v*` 列表里它的下一项；都没有就跳过这个 tag
   （不扫全历史）。
2. `git log <上一个 tag>..<oid> --format=%B`，只认**行首**的 `Fixes` / `Closes` / `Resolves`
   后面跟的 `PYTHON-<ID>`。正文别处提到的编号不算（否则改一句注释、提一下问题号就会关 issue）。
3. **状态从本地来**（默认不联网）：本工具自己关过的记录
   （`temp/sentry/issue-resolve.json`，`--apply` 成功或 `--status` 观察后写）与本地遥测库
   （`temp/telemetry/issues/<SHORT>.json`）的卡片，两者按**时刻取最新**。`resolved` / `ignored`
   的列进「已关闭」、不写远端；没有记录或状态未知的留在「待关闭」——不猜"已经关了"。
4. **`--status` 才联网核对**：`GET /api/0/organizations/<org>/shortids/<SHORT-ID>/` 一次请求
   同时带回 `groupId` 与 `group.status`（少数部署不带 `group` 时才再
   `GET /api/0/issues/<groupId>/`），核对完把结果写回本地记录，下次离线也能答。
5. **写远端**：`PUT /api/0/issues/<groupId>/` 带 `{"status": "resolved"}`。直接把 short ID
   塞 `/api/0/issues/<ID>/` 会 404。只改 `status`，不加备注、不动事件、不删 issue。
   `groupId` 优先用本地记录里缓存的，没有再 GET 一遍 shortids（离线 `--apply` 因此只有 PUT）。

## 为什么默认不联网

联网一次要 token、要代理、要等超时——`issue-resolve` 的主要用法（push 后问"关了吗"）
不该依赖它。本地能答的部分就本地答：

- hook 装成 `--apply` 时 **push 新 tag 就关过一遍**，Sentry 的提交关联也会按
  `Fixes PYTHON-XX` 关。这些动作在 `--apply` 成功后写进 `temp/sentry/issue-resolve.json`，
  下次手动跑 list-only 直接读它。
- 本地遥测库（画图用的那个库）默认只留未解决的 issue，它的卡片写着"上次刷新时还没关"。
  两个来源都带时刻，谁新算谁——push 之后库还没刷新时，卡片说 unresolved、关闭记录说
  resolved，取关闭记录。

实测旧版（只看提交正文，或每次都联网）：`v1.0.0-beta.71` 推送后 `PYTHON-2T / 2P / 34 / 32`
在 Sentry 上全是 `resolved`，清单仍列四个「待关闭」并写"hook 加 --apply 才实际关闭"。

现在的清单：`已关闭（<来源>（<时刻>））：…` 与 `待关闭: …`；都关过了就打
`待关闭: （无）` + `没有需要处理的 issue（已关闭 N 条）`。提示还会看已装 hook 的模式：hook
已是 `apply` 时说"push 新 tag 时会自动关闭上面「待关闭」的 issue"，没装或仍是 list-only 时
才说"加 --apply / --install --apply"。

本地一条记录都没有时，清单会说清"没有本地状态记录（只有这个工具关过的才留记录）"并提示
`--status`；**不会**因为"库里没有这条"就当成已关闭（超期、从没上报过都会不在库里）。

`--only`（验证 REST 分支）不看任何状态——它是"按显式给的 ID 原样处理"的开关，不该被已
resolved 挡住。`--status` 连续失败 2 次就收手，pre-push 仍恒 exit 0。

## 离线 `--apply` 为什么重写已关闭的

已关闭的 issue 仍会被 `--apply` 幂等 PUT 一次：issue 可能被 Sentry 自动 reopened，跳过就会
漏关。只跳过 `ignored`（那是有意"不修"的状态，不能被 resolved 覆盖）。终端会点明这点：
`本地记录说这些已关闭，仍幂等重写一次：…`。带 `--status` 时按核对结果写，已关闭的不再重复 PUT。

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

`python -m tools.sentry.cli issue-resolve` 直接跑时没有 `pre-push` 输入，算的是**最新 tag
之后**的提交（`<最新 tag>..HEAD`）——要发的版本还没打 tag，这才是"这次会关掉哪些 issue"。
最新 tag 之后没有提交时（HEAD 就停在 tag 上），退回这个 tag 自己那段
（`<次新 tag>..<最新 tag>`）。终端会写明这次按哪一段算：

```
[sentry-issues] 没有 pre-push 输入（手动运行）：按最新 tag 之后的提交算（v1.0.0-beta.71..HEAD，8 个）
[sentry-issues] v1.0.0-beta.71..HEAD: PYTHON-M, PYTHON-3D, PYTHON-3A, PYTHON-3B
```

先出清单，确认后 `--apply` 才写远端。清单里的「已关闭」来自本地记录与本地遥测库（见上
「为什么默认不联网」），不联网。两段都没得算时（最早的 tag、且它之后没有新提交）跳过——
不扫全历史。看到 `没有新建的 v* tag，跳过` 说明 stdin 里有行但不是新建 tag（例如只推分支）。

要处理某个 tag 自己的提交（补跑、重放某个已发版本）用 `--tag NAME`，可重复：

```bash
python -m tools.sentry.cli issue-resolve --tag v1.0.0-beta.70
python -m tools.sentry.cli issue-resolve --tag v1.0.0-beta.70 --tag v1.0.0-beta.71 --apply
```

`--tag` 名字不在本地时只告警并跳过（先 `git fetch --tags`）。

**绘图侧复用这段范围**：`tools/drawing/lib/cases/release_mark.py` 调
`fixed_in_unreleased()`（同一条范围、同一套 `Fixes/Closes/Resolves` 提取，静默不打日志），
给"提交已修、还没发版"的 issue 产物名加 `_已修复待发版`。两边口径必须一致——一边改了
范围另一边就要同批改，否则会出现"图说没修、关 issue 说修了"。标记只是改名，不重画图。

## 默认只列清单

`--install` 默认生成 **list-only** 的 hook：只打印"新 tag / 扫到哪些 issue / 已关闭 / 待关闭"，
不写远端（状态读本地，不联网）。确认无误后再 `--install --apply`。两种 hook 都**恒 exit 0**
（含内部异常），任何失败只告警、不阻断 push——`pre-push` 非零退出会拦下整个 push，那不是一个
"关 issue" 的脚本该做的事。

## 验证与排查

- `--check`：打印仓库、`config.json` 的 target、org、hook 路径与模式、token 来源（只显示有没有，
  不打印 token）。
- `--ref LINE`（可重复）/ `--stdin-file`：不用真 push 就能模拟 hook 输入，例如
  `--ref "refs/tags/v1.0.0-beta.70 <oid> refs/tags/v1.0.0-beta.70 000…0"`。
- `--tag NAME`：不模拟 stdin，直接指定要处理的本地 tag（处理它自己的提交范围，见上「手动跑」）。
  和手动直跑一样默认只列清单。
- `--only --issue <SHORT-ID>`：只用给定的 ID、**不扫提交范围**，也不看状态。验证 REST
  分支时用它——`--issue` 是**追加**语义，不带 `--only` 会把范围里扫到的真实 issue 一起关掉
  （踩过：验证 404 分支时误关了两条，见 `.agents/procedures/_inbox.md`）。
- `--status`：额外联网核对一次（一次 shortids GET 同时带回 `groupId` 与 `status`），并把结果
  与 `groupId` 写进本地记录，之后离线也能答、离线 `--apply` 也少一半请求。
- 本地记录在 `temp/sentry/issue-resolve.json`（不入库）：`{short_id: {status, at, by, group}}`，
  想让它重来一遍就删这个文件。
- 纯函数自检在 `python -m tools.sentry.cli selftest`（`lib/selftests/issue_resolve.py`）。

## 换位置或升级之后

hook 里写的是 `cd <仓库根>` + `python -m tools.sentry.cli issue-resolve`，所以脚本搬目录**不用**
改 hook；但换了 Python 解释器、或从旧版（直接指向脚本文件）升上来时要重跑 `--install`
（`--install --apply` 保持 apply 模式）。`--uninstall` 只删除带
`# managed-by: sentry_issue_resolve` 标记的 hook，别人的 hook 不会被覆盖或删掉（除非 `--force`）。
