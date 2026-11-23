/**
 * SPDX-License-Identifier: AGPL-3.0-only
 * Derived from MAA1999/M9A tools/android-packaging.mjs (retrieved 2026-11-23).
 * Upstream authors: M9A contributors. Licensed under GNU AGPL v3, NOT the
 * surrounding repository's MIT license. Original license and source:
 * https://github.com/MAA1999/M9A/blob/main/LICENSE
 * https://github.com/MAA1999/M9A/blob/main/tools/android-packaging.mjs
 * Local adaptations: assets/interface.json name, requirements-only version,
 * explicit fallback opt-in, semantic prerelease ordering, offline tests.
 * This notice does not relicense the upstream work.
 */
import {appendFileSync, existsSync, readFileSync} from "node:fs";
import {join, resolve} from "node:path";
import {pathToFileURL} from "node:url";

const INTERFACE = "assets/interface.json";
const REQUIREMENTS = "requirements.txt";

// 内核 repo：Android 绑定随它的 release 发布，tag 形如 "3.13.15-maafw5.12.3"（第一段是 CPython 版本）
const CORE_REPO = "AliothMoon/MaaAgentCoreAndroid";
// MaaFramework releases are queried only to recover actual prerelease tag spelling.
const MAAFW_REPO = "MaaXYZ/MaaFramework";

const CORE_TAG_ENV = "AGENT_CORE_TAG";
const RELEASES_FIXTURE_ENV = "ANDROID_PACKAGING_RELEASES_FIXTURE";
const CHAQUOPY_FIXTURE_ENV = "ANDROID_PACKAGING_CHAQUOPY_FIXTURE";
// 配对 tag 的版本段可以是预发布拼写（3.13.15-maafw5.15.0-beta.1），`\w` 不含 `-`，必须显式放行
const CORE_MAAFW_PATTERN = /^(\d[\w.]*)-maafw([A-Za-z0-9][A-Za-z0-9.+-]*)$/;

const SAFE_PREFIX_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
// requirements 行首的「包名==精确版本」；uv export 的 --hash 续行（缩进 + --）、注释、-r / -e
// 与非精确行（比较符不是 ==）都不匹配。uv export 恒为精确钉版，非精确行没有比对的基准
const REQUIREMENT_ENTRY_PATTERN = /^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)/;

// Chaquopy 的版本化 PyPI 索引。键 = 内核 CPython 的 major.minor；"13.1" 是 Chaquopy 自己的
// 发布线编号，不是 CPython 版本，没有 CPython→URL 的公式，只能手维护（上游硬编码位置：
// MaaFwApp scripts/build_agent_bundle.py 的文档示例写死 pypi-13.1）。
// 注意 build_agent_bundle.py 的 pypi-upstream 永远在索引列表里，本表只是追加版本化索引。
const CHAQUOPY_INDEX_BY_CPYTHON = new Map([
    [
        "3.13",
        "https://chaquo.com/pypi-13.1/",
    ],
]);

// 人工兜底：自动探测的结果不对时（索引页面异常、要强制别的版本）在这里钉死版本。语义与自动
// pin 相同，仍只对 requirements 里实际出现的包生效；平时应为空——常规维护是修探测，不是加行
const CHAQUOPY_PIN_OVERRIDES = new Map([]);

// 内核自带的包（agent-core.json 的 provides，今天 = maafw 绑定 / numpy / StrEnum）：
// build_agent_bundle.py 会把 requirements 里这些包整包丢弃（core wins），它们不进 pip，
// 探测它们没有意义——对着索引降级内核自带的包，--require 会把旧版装回内核 site-packages
// 旁边跟内核打架。内核新增自带包时同步这张表（manifest 在内核 tarball 内，无轻量获取渠道）
const CORE_PROVIDES = new Set([
    "maafw",
    "numpy",
    "strenum",
]);

function readText(path) {
    return existsSync(path) ? readFileSync(path, "utf8") : undefined;
}

function parseJsonc(text) {
    // Mask comments without touching quoted URLs or escaped quotes; then mask
    // trailing commas only outside strings. JSON.parse still validates syntax.
    const pattern = /"(?:\\.|[^"\\])*"|\/\/[^\r\n]*|\/\*[\s\S]*?\*\//g;
    const stripped = text.replace(/^\uFEFF/, "").replace(pattern, (token) =>
        token.startsWith('"') ? token : token.replace(/[^\r\n]/g, " "));
    return JSON.parse(stripped.replace(/"(?:\\.|[^"\\])*"|,(\s*[}\]])/g,
        (token, closing) => closing === undefined ? token : closing));
}

function readObject(path) {
    const text = readText(path);
    if (text === undefined) throw new Error(`${path} 不存在`);
    const parsed = parseJsonc(text);
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error(`${path} 的顶层必须是对象`);
    }
    return parsed;
}

function compareVersions(a, b) {
    const left = a.split(".").map(Number);
    const right = b.split(".").map(Number);
    for (let i = 0; i < Math.max(left.length, right.length); i += 1) {
        const diff = (left[i] ?? 0) - (right[i] ?? 0);
        if (diff !== 0) return diff;
    }
    return 0;
}

function envTrim(name) {
    return (process.env[name] ?? "").trim();
}

async function githubReleases(repo, perPage = 100) {
    const headers = {
        Accept: "application/vnd.github+json",
        "User-Agent": "android-packaging-resolver",
    };
    if (process.env.GITHUB_TOKEN) headers.Authorization = `Bearer ${process.env.GITHUB_TOKEN}`;
    const response = await fetch(`https://api.github.com/repos/${repo}/releases?per_page=${perPage}`, {headers, signal: AbortSignal.timeout(8000)});
    if (!response.ok) {
        const hint = response.status === 403 ? "（可能是 API 限流，确认 GITHUB_TOKEN 已传入）" : "";
        throw new Error(`查询 ${repo} 的 releases 失败：HTTP ${response.status}${hint}`);
    }
    return response.json();
}

async function loadFixture() {
    const path = process.env[RELEASES_FIXTURE_ENV];
    if (path === undefined || path === "") return null;
    return JSON.parse(readFileSync(path, "utf8"));
}

/** Read on every resolution so environment fixtures cannot leak between tests. */
function chaquopyFixturePackages() {
    const path = envTrim(CHAQUOPY_FIXTURE_ENV);
    return path === "" ? null : readObject(path);
}

async function fetchCoreTags() {
    const fixture = await loadFixture();
    if (fixture !== null) return fixture.core ?? [];
    return (await githubReleases(CORE_REPO)).map((release) => release.tag_name);
}

async function fetchMaafwReleases() {
    const fixture = await loadFixture();
    if (fixture !== null) return fixture.maafw ?? [];
    return (await githubReleases(MAAFW_REPO, 30)).map((release) => ({
        tag: release.tag_name,
        prerelease: release.prerelease === true,
    }));
}

/** 内核 repo 现有的全部 maafw 配对版本，从新到旧，给报错信息指路；同一版本两种拼写时只留一个 */
function availablePairings(coreTags) {
    const versions = new Map();
    for (const tag of coreTags) {
        const match = CORE_MAAFW_PATTERN.exec(tag);
        if (match !== null) versions.set(maafwVersionKey(match[2]), match[2]);
    }
    return [...versions.values()].sort(compareMaafwVersions).reverse();
}

/** 预发布后缀归一成 PEP 440 的短形式：`-beta.1` / `beta.1` / `b1` 都是 `b1`，`rc.2` / `c2` 都是
 *  `rc2`。同一个版本在 requirements（uv 导出的归一形式）与内核 tag（GitHub tag 原文）里拼写不同，
 *  比较前必须先落到同一个 key */
function maafwPrereleaseKey(text) {
    const suffix = text.toLowerCase().replace(/^[-_.]+/, "");
    if (suffix === "") return "";
    const match = /^(alpha|beta|preview|pre|rc|a|b|c)[-_.]?(\d*)(.*)$/.exec(suffix);
    if (match === null) return suffix;
    const word = match[1];
    const short = word === "a" || word === "alpha" ? "a" : word === "b" || word === "beta" ? "b" : "rc";
    return `${short}${match[2]}${match[3]}`;
}

/** 版本比较用的 key：`5.15.0-beta.1`、`5.15.0b1`、`v5.15.0b1` 归一到 `5.15.0b1`。解析不了的退回
 *  小写原文（仍可比较，只是没有发布线语义） */
function maafwVersionKey(text) {
    const value = String(text).trim().replace(/^[vV]/, "");
    const match = /^(\d+(?:\.\d+)*)(.*)$/.exec(value);
    if (match === null) return value.toLowerCase();
    return `${match[1]}${maafwPrereleaseKey(match[2])}`;
}

/** MaaFW 版本拆成「数字段 + 后缀」：数字段决定发布线（5.14.3 / 5.13.0b6 / 5.15.0-beta.1），
 *  后缀只在同一条线内比先后（已归一，`b1` 与 `beta.1` 不会比出先后）。解析不了的（不以数字开头）返回 undefined */
function parseMaafwVersion(text) {
    const value = String(text).trim().replace(/^[vV]/, "");
    const match = /^(\d+(?:\.\d+)*)(.*)$/.exec(value);
    if (match === null) return undefined;
    return {release: match[1].split(".").map(Number), suffix: maafwPrereleaseKey(match[2])};
}

/** 正式版高于预发布；预发布按 alpha < beta < rc，再按数字编号比较。 */
function compareMaafwVersions(left, right) {
    const a = parseMaafwVersion(left);
    const b = parseMaafwVersion(right);
    if (a === undefined || b === undefined) return compareVersions(left, right);
    const release = compareVersions(a.release.join("."), b.release.join("."));
    if (release !== 0) return release;
    if (a.suffix === b.suffix) return 0;
    if (a.suffix === "") return 1;
    if (b.suffix === "") return -1;
    const stage = (suffix) => {
        const match = /^(a|b|rc)(\d*)$/.exec(suffix);
        if (match === null) throw new Error(`Unsupported prerelease: ${suffix}`);
        return [{a: 0, b: 1, rc: 2}[match[1]], Number(match[2] || 0)];
    };
    const x = stage(a.suffix), y = stage(b.suffix);
    return x[0] - y[0] || x[1] - y[1];
}

/** candidate 必须在完整版本顺序上不高于 pin；固定 beta 时不会选择同线正式版。 */
function releaseAtMost(candidate, pin) {
    const a = parseMaafwVersion(candidate);
    const b = parseMaafwVersion(pin);
    if (a === undefined || b === undefined) return false;
    return compareMaafwVersions(candidate, pin) <= 0;
}

/** 挑可用的配对内核：绑定与原生库必须同版本，所以 client 原生库总跟着选中的绑定走。
 *  优先精确配对；内核还没跟上上游补丁（requirements 已 bump、配对还没发）时降级到不高于固定
 *  版本的最高配对——APK 里前后端仍同版本，漂移的只是相对 requirements 的版本差。固定版本比内核
 *  全部配对都旧时不做「升级」猜测（那多半是版本写错了），仍报错并列出现有配对 */
function pickPackagingCore(coreTags, requested, allowFallback = false) {
    const pairings = availablePairings(coreTags);
    const requestedKey = maafwVersionKey(requested);
    const exact = pairings.find((version) => maafwVersionKey(version) === requestedKey);
    const eligible = pairings.filter((version) => releaseAtMost(version, requested)).sort(compareMaafwVersions);
    if (exact === undefined && !allowFallback) {
        throw new Error(`No exact Android core pairing for maafw${requested}; available: ${pairings.join(", ") || "none"}. ` +
            "Use --allow-fallback or ANDROID_PACKAGING_ALLOW_FALLBACK=1 to explicitly allow an older pairing.");
    }
    const chosen = exact ?? eligible.at(-1);
    if (chosen === undefined) {
        throw new Error(
            `内核（${CORE_REPO}）没有不高于 maafw${requested} 的配对 release；` +
                `现有配对：${pairings.join("、") || "无"}。` +
                `确认 requirements.txt 里的版本没写错，` +
                `或等内核发布即可，救急可用 ${CORE_TAG_ENV} 指定别的内核 release`,
        );
    }
    return {
        coreTag: coreTags.filter((tag) => {
            const match = CORE_MAAFW_PATTERN.exec(tag);
            return match !== null && maafwVersionKey(match[2]) === maafwVersionKey(chosen);
        }).sort((a, b) => compareVersions(a.split("-maafw")[0], b.split("-maafw")[0])).at(-1),
        maafwVersion: chosen,
        degraded: maafwVersionKey(chosen) !== requestedKey,
    };
}

/** 下载地址用的是 release tag 的原文：内核 tag 与 requirements 的版本段可能是归一形式
 *  （`5.15.0b1`），GitHub release 却是 `v5.15.0-beta.1`。预发布按 release 列表挑真实 tag（列表
 *  必须在 release 列表找到真实 tag，否则失败；正式版没有拼写歧义，不需要这次查询 */
async function maafwReleaseTag(version) {
    const parsed = parseMaafwVersion(version);
    if (parsed === undefined || parsed.suffix === "") return `v${version}`;
    const releases = await fetchMaafwReleases();
    const key = maafwVersionKey(version);
    const match = releases.find((release) => maafwVersionKey(String(release.tag)) === key);
    if (match === undefined) throw new Error(`MaaFramework release tag not found for ${version}`);
    return match.tag;
}

function artifactPrefix(interfaceConfig) {
    const name = interfaceConfig.name;
    if (typeof name !== "string" || !SAFE_PREFIX_PATTERN.test(name.trim())) {
        throw new Error("assets/interface.json name must be a safe ASCII artifact prefix");
    }
    return name.trim();
}

function agentDeclared(interfaceConfig) {
    const value = interfaceConfig.agent;
    return Array.isArray(value) ? value.length > 0 : value !== null && typeof value === "object";
}

function parseRequirementPin(text) {
    const entries = (text ?? "").split(/\r?\n/).filter((line) => /^maafw\s*(?:[=<>!~;]|$)/i.test(line));
    if (entries.length !== 1) throw new Error("requirements.txt must contain exactly one unconditional maafw==VERSION pin");
    const match = /^maafw==([0-9]+(?:\.[0-9]+){2}(?:(?:a|b|rc)[0-9]+|-(?:alpha|beta|rc)\.[0-9]+)?)(?:\s*(?:#.*)?)$/i.exec(entries[0]);
    if (match === null) throw new Error("requirements.txt maafw must use one exact, unconditional supported version");
    return match[1];
}

function normalizeRequirementName(name) {
    // PEP 503：大小写与 - _ . 归一
    return name.toLowerCase().replace(/[-_.]+/g, "-");
}

/** requirements 里的精确钉版条目（含 maafw），保持文件顺序。带 `;` 环境标记的行整行跳过：
 *  Android 上 pip 不会装它（如 win32 专属），替它降级反而会凭空多装一个包 */
function requirementEntries(text) {
    if (text === undefined) return [];
    const entries = [];
    for (const line of text.split(/\r?\n/)) {
        if (line.includes(";")) continue;
        const match = REQUIREMENT_ENTRY_PATTERN.exec(line);
        if (match === null) continue;
        entries.push({name: normalizeRequirementName(match[1]), version: match[2]});
    }
    return entries;
}

/** 抓索引上某包的 wheel 文件名。索引只放 Chaquopy 自家构建的 Android wheel（纯 Python 包走
 *  upstream），404 = 没这个包，属正常；其他失败大声报错——静默跳过会把错误拖到构建中途的 pip */
async function fetchChaquopyWheels(indexUrl, name) {
    const fixture = chaquopyFixturePackages();
    if (fixture !== null) return Array.isArray(fixture[name]) ? fixture[name] : [];
    const response = await fetch(`${indexUrl}${name}/`, {
        headers: {"User-Agent": "android-packaging-resolver"},
        signal: AbortSignal.timeout(8000),
    });
    if (response.status === 404) return [];
    if (!response.ok) {
        throw new Error(`查询 Chaquopy 索引的 ${name} 失败：HTTP ${response.status}（${indexUrl}${name}/）`);
    }
    const text = await response.text();
    // PEP 503 simple 页面：wheel 全在 <a> 锚文本里（href 带 #sha256 尾巴，不能直接取）
    return [...text.matchAll(/>([^<>]+\.whl)</g)].map((match) => match[1]);
}

/** wheel 文件名拆出版本与 python 标签：{dist}-{version}-{python}-{abi}-{platform}.whl。已知
 *  dist 前缀（PEP 427 里 - 转写成 _）。version 后面可能带 build tag（numpy-1.26.2-0-cp313），
 *  build 段不以 cp/py/pp 开头，据此让位 */
function parseWheel(filename, distribution) {
    const prefix = `${distribution.replace(/-/g, "_")}-`;
    if (!filename.startsWith(prefix) || !filename.endsWith(".whl")) return undefined;
    const segments = filename.slice(prefix.length).slice(0, -4).split("-");
    const tagIndex = segments.findIndex((segment) => /^(cp|py|pp)/.test(segment));
    if (tagIndex < 1) return undefined;
    return {version: segments[0], pythonTag: segments[tagIndex]};
}

/** 该包在索引上可装的版本集合；py3 / py2.py3 纯 wheel 任何 CPython 都可装 */
function indexVersions(wheels, distribution, cpTag) {
    const versions = new Set();
    for (const wheel of wheels) {
        const parsed = parseWheel(wheel, distribution);
        if (parsed === undefined) continue;
        if (parsed.pythonTag === cpTag || parsed.pythonTag === "py3" || parsed.pythonTag === "py2.py3") {
            versions.add(parsed.version);
        }
    }
    return versions;
}

/** 逐包探测 Chaquopy 索引，生成 build_agent_bundle.py 的 --exclude/--require 参数串：桌面
 *  pin 在索引上有 cp 兼容构建 → 不动；没有 → 钉到索引最高可用版；索引上没这个包 → 纯
 *  Python 走 upstream，不钉。只处理 requirements 里实际出现的包，不给别的项目强加降级 */
async function chaquopyPins(indexUrl, entries, cpTag) {
    const args = [];
    for (const {name, version} of entries) {
        const override = CHAQUOPY_PIN_OVERRIDES.get(name);
        if (override !== undefined) {
            args.push(`--exclude ${name}`, `--require ${name}==${override}`);
            continue;
        }
        if (CORE_PROVIDES.has(name)) continue;
        const wheels = await fetchChaquopyWheels(indexUrl, name);
        if (wheels.length === 0) continue;
        const versions = indexVersions(wheels, name, cpTag);
        if (versions.size === 0) {
            throw new Error(
                `Chaquopy 索引有 ${name} 的构建但没有 ${cpTag} 兼容的（${indexUrl}${name}/），` +
                    `Android 装不进这个原生包；到上游确认后换版本，或在 CHAQUOPY_PIN_OVERRIDES 兜底`,
            );
        }
        if (version !== undefined && versions.has(version)) continue;
        const best = [...versions].sort(compareVersions).at(-1);
        args.push(`--exclude ${name}`, `--require ${name}==${best}`);
    }
    return args.join(" ");
}

/** 内核 tag 第一段是 CPython 版本，取 major.minor 查手维护的 Chaquopy 索引表 */
function chaquopyIndex(coreTag) {
    if (coreTag === "") return "";
    const [cpython] = coreTag.split("-");
    const minor = cpython.split(".").slice(0, 2).join(".");
    const url = CHAQUOPY_INDEX_BY_CPYTHON.get(minor);
    if (url === undefined) {
        const known = [...CHAQUOPY_INDEX_BY_CPYTHON.keys()].join("、") || "无";
        throw new Error(
            `Chaquopy 索引表里没有 CPython ${minor} 的条目（内核 tag ${coreTag}）；已知：${known}。` +
                `这是手维护表（无 CPython→URL 公式），到上游确认发布线后在本表补一行`,
        );
    }
    return url;
}

/** 同一内核 tag 推出 wheel 的 python 标签：3.13.15 → cp313 */
function coreCpTag(coreTag) {
    const [
        major,
        minor,
    ] = coreTag.split("-")[0].split(".");
    return `cp${major}${minor}`;
}

async function resolvePackaging(root, {allowFallback = envTrim("ANDROID_PACKAGING_ALLOW_FALLBACK") === "1"} = {}) {
    const interfaceConfig = readObject(join(root, INTERFACE));
    const hasAgent = agentDeclared(interfaceConfig);
    // This repository populates OCR from MaaCommonAssets in the packaging job.
    const hasOcr = true;
    const prefix = artifactPrefix(interfaceConfig);
    const warnings = [];
    const requirementsText = readText(join(root, REQUIREMENTS));
    const requirementPin = parseRequirementPin(requirementsText);
    let maafwVersion = requirementPin;

    // 内核（绑定载体）只在 agent 项目里用；纯 pipeline 项目 client 库直接从 MaaFramework release 铺
    let coreTag = "";
    let coreFallback = "";
    if (hasAgent) {
        const override = envTrim(CORE_TAG_ENV);
        if (override !== "") {
            const match = CORE_MAAFW_PATTERN.exec(override);
            if (match === null) {
                throw new Error(`${CORE_TAG_ENV}=${override} 里解析不到 -maafw 版本段`);
            }
            coreTag = override;
            if (maafwVersionKey(match[2]) !== maafwVersionKey(maafwVersion)) {
                if (!allowFallback || !releaseAtMost(match[2], maafwVersion)) {
                    throw new Error(`${CORE_TAG_ENV} must match requirements pin unless an older version is explicitly allowed`);
                }
                coreFallback = `maafw${maafwVersion} → maafw${match[2]}`;
                warnings.push(`${CORE_TAG_ENV}: ${coreFallback}`);
                maafwVersion = match[2];
            }
        } else {
            const picked = pickPackagingCore(await fetchCoreTags(), maafwVersion, allowFallback);
            coreTag = picked.coreTag;
            if (picked.degraded) {
                coreFallback = `maafw${maafwVersion} → maafw${picked.maafwVersion}`;
                warnings.push(
                    `内核（${CORE_REPO}）还没出 maafw${maafwVersion} 配对的 release，降级到最高可用配对 ` +
                        `maafw${picked.maafwVersion}；APK 里绑定与原生库仍同版本，只是比 requirements 固定的版本旧。` +
                        `内核发版后这次降级会自动消失`,
                );
                maafwVersion = picked.maafwVersion;
            }
        }
    }

    // pins 依赖索引与内核 CPython，纯 pipeline 项目没有 agent 运行时，不探测
    const chaquopyIndexUrl = chaquopyIndex(coreTag);
    const pins =
        chaquopyIndexUrl === ""
            ? ""
            : await chaquopyPins(chaquopyIndexUrl, requirementEntries(requirementsText), coreCpTag(coreTag));

    return {
        coreTag,
        coreFallback,
        maafwTag: await maafwReleaseTag(maafwVersion),
        artifactPrefix: prefix,
        requirementPin,
        hasAgent,
        hasOcr,
        chaquopyIndex: chaquopyIndexUrl,
        chaquopyPins: pins,
        warnings,
    };
}

function writeOutputs(pairs) {
    const output = process.env.GITHUB_OUTPUT;
    if (output === undefined || output === "") return;
    appendFileSync(
        output,
        Object.entries(pairs)
            .map(
                ([
                    key,
                    value,
                ]) => `${key}=${value}\n`,
            )
            .join(""),
        "utf8",
    );
}

async function main(argv = process.argv.slice(2)) {
    if (argv.includes("--help") || argv.includes("-h")) {
        console.log(`Usage: node tools/android-packaging.mjs [root] [--allow-fallback]
Resolve Android core/client versions from requirements.txt and artifact prefix from assets/interface.json.
Default: require an exact core pairing; fallback may only select an older version.
Environment: GITHUB_TOKEN, GITHUB_OUTPUT, AGENT_CORE_TAG,
  ANDROID_PACKAGING_ALLOW_FALLBACK=1 (equivalent explicit opt-in),
  ANDROID_PACKAGING_RELEASES_FIXTURE={path to JSON: core:[tags], maafw:[{tag,prerelease}]},
  ANDROID_PACKAGING_CHAQUOPY_FIXTURE={path to JSON: normalized-package:[wheel filenames]}.
Outputs: core_tag, core_fallback, maafw_tag, artifact_prefix, has_agent, has_ocr,
  chaquopy_index, chaquopy_pins. No dependencies; Node 22+. Network timeout: 8 seconds.`);
        return 0;
    }
    const positional = argv.filter((arg) => !arg.startsWith("--"));
    if (positional.length > 1 || argv.some((arg) => arg.startsWith("--") && arg !== "--allow-fallback")) {
        console.error("Unknown arguments; use --help");
        return 1;
    }
    const root = positional[0] ?? process.cwd();
    let packaging;
    try {
        packaging = await resolvePackaging(root, {allowFallback: argv.includes("--allow-fallback") || envTrim("ANDROID_PACKAGING_ALLOW_FALLBACK") === "1"});
    } catch (error) {
        console.log(`::error::${error instanceof Error ? error.message : String(error)}`);
        return 1;
    }

    for (const warning of packaging.warnings) console.log(`::warning::${warning}`);

    console.log(`agent core tag   : ${packaging.coreTag || "（纯 pipeline，无内核）"}`);
    console.log(`core fallback    : ${packaging.coreFallback || "（未降级）"}`);
    console.log(`client MaaFW tag : ${packaging.maafwTag}`);
    console.log(`artifact prefix  : ${packaging.artifactPrefix}`);
    console.log(`requirements pin : ${packaging.requirementPin ?? "（未钉 maafw==X）"}`);
    console.log(`has agent        : ${packaging.hasAgent}`);
    console.log(`has ocr (re-sync): ${packaging.hasOcr}`);
    console.log(`chaquopy index   : ${packaging.chaquopyIndex || "（无 agent 运行时）"}`);
    console.log(`chaquopy pins    : ${packaging.chaquopyPins || "（无需降级）"}`);

    writeOutputs({
        core_tag: packaging.coreTag,
        core_fallback: packaging.coreFallback,
        maafw_tag: packaging.maafwTag,
        artifact_prefix: packaging.artifactPrefix,
        has_agent: String(packaging.hasAgent),
        has_ocr: String(packaging.hasOcr),
        chaquopy_index: packaging.chaquopyIndex,
        chaquopy_pins: packaging.chaquopyPins,
    });
    return 0;
}

// 不能用 process.exit()：Windows 上它与 fetch 留下的 libuv 句柄析构竞争会断言崩溃、
// 退出码非零。改设 exitCode 让事件循环排空后自然退出（保持连接会在几秒内被 undici 回收）
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) main()
    .then((code) => {
        process.exitCode = code;
    })
    .catch((error) => {
        console.log(`::error::${error instanceof Error ? error.message : String(error)}`);
        process.exitCode = 1;
    });

export {CORE_PROVIDES, compareMaafwVersions, maafwVersionKey, pickPackagingCore,
    parseRequirementPin, requirementEntries, indexVersions, chaquopyPins,
    resolvePackaging, artifactPrefix, agentDeclared, main};
