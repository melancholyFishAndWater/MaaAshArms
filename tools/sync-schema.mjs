// 把 deps/tools/ 下的 schema 快照替换成 MaaFramework 上游指定 ref 的版本。
//
// 背景：deps/tools/*.schema.json 是 boilerplate 带来的快照，仓库的工作流不会自动同步
// （.agents/project.md）。这个脚本负责那一步：按 ref 拉取、先验证关键字段再落盘，并写下
// 来源 ref 与每个文件的 sha256。**它不判断"哪边更新"**，只做替换与记录。
//
// 重要：上游 main / tag 里的 schema 不一定比本仓库新。实测 MaaXYZ/MaaFramework@main 的
// interface_import / pipeline schema 比本仓库快照旧（缺 hotkey、min_count/max_count 等），
// 所以默认 ref 故意不是 main，跑之前先想清楚要拉到哪个版本。
//
// 用法：
//   MAA_SCHEMA_REF=main npm run sync:schema          # 拉到上游 main（可能是一次降级）
//   MAA_SCHEMA_REF=<commit-sha> npm run sync:schema  # 钉到某个提交，可复现
//   node tools/sync-schema.mjs --local-only          # 不联网，只按本地文件重写 manifest
//   npm run sync:schema                              # 不设变量时会报错退出，避免误覆盖
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { mkdir, rename, writeFile, readFile } from "node:fs/promises";
import { join } from "node:path";

const upstreamRepo = "MaaXYZ/MaaFramework";
const upstreamRef = process.env.MAA_SCHEMA_REF?.trim() ?? "";
const targetDir = "deps/tools";
const baseUrl = `https://raw.githubusercontent.com/${upstreamRepo}/${upstreamRef}/tools`;

// 上游 tools/ 下这 6 个文件，deps/tools/ 本地都有副本；interface_config.schema.json 与
// 两个 custom.*.schema.json 虽然不被 tools/validate_schema.py 的 --schema 入口直接使用，
// 但它们会进 schema_store 参与 $ref 解析，所以必须同版本。
const schemas = [
    {
        path: "interface.schema.json",
        validate: (json) => {
            assertEqual(json.title, "MaaFramework Project Interface V2", "interface schema title");
            assertEqual(
                json.properties?.interface_version?.const,
                2,
                "interface_version const",
            );
        },
    },
    {
        path: "interface_import.schema.json",
        validate: (json) => assertRecord(json.properties?.task, "interface import task property"),
    },
    {
        path: "interface_config.schema.json",
        validate: (json) => assertRequired(json.required, "controller", "interface config"),
    },
    {
        path: "pipeline.schema.json",
        validate: (json) => assertRecord(json.$defs?.Node, "pipeline Node definition"),
    },
    {
        path: "custom.action.schema.json",
        validate: (json) => assertRecord(json.properties, "custom action properties"),
    },
    {
        path: "custom.recognition.schema.json",
        validate: (json) => assertRecord(json.properties, "custom recognition properties"),
    },
];

await main();

async function main() {
    await mkdir(targetDir, { recursive: true });

    if (process.argv.includes("--local-only")) {
        await writeManifest(
            schemas.map((schema) => ({
                path: schema.path,
                url: null,
                sha256: sha256(readFileSync(join(targetDir, schema.path), "utf8")),
            })),
            { repository: upstreamRepo, ref: null, note: "本地快照的哈希记录，未与上游比对" },
        );
        console.log(`\n已按本地文件写入 ${targetDir}/schema-manifest.json（未联网比对）`);
        return;
    }

    if (!upstreamRef) {
        console.error(
            "需要显式指定上游 ref，避免误把本仓库快照覆盖成更旧的版本：\n" +
                "  MAA_SCHEMA_REF=main npm run sync:schema\n" +
                "  MAA_SCHEMA_REF=<commit-sha> npm run sync:schema\n" +
                "只想按本地文件重写 manifest 时用：node tools/sync-schema.mjs --local-only",
        );
        process.exit(1);
    }

    const files = [];
    const changed = [];

    // 先把 6 个文件全部抓下来并验证，再统一落盘：任何一步失败都不留下半套 schema。
    for (const schema of schemas) {
        const text = await fetchSchemaText(schema.path);
        let json;
        try {
            json = JSON.parse(text);
        } catch (error) {
            throw new Error(`${schema.path} 不是合法 JSON：${error.message}`);
        }
        schema.validate(json);

        const content = `${JSON.stringify(json, null, 4)}\n`;
        files.push({ path: schema.path, url: `${baseUrl}/${schema.path}`, sha256: sha256(content), content });
    }

    for (const file of files) {
        const target = join(targetDir, file.path);
        const before = await readFileOrNull(target);
        if (before === file.content) {
            console.log(`unchanged  ${file.path}`);
            continue;
        }

        // 原子写：临时文件 + rename，避免中断留下半个 180 KB 的 JSON。
        const tmp = `${target}.tmp`;
        await writeFile(tmp, file.content, "utf8");
        await rename(tmp, target);
        changed.push(file.path);
        console.log(`changed    ${file.path}${before === null ? " (新增)" : ""}`);
    }

    await writeManifest(files, { repository: upstreamRepo, ref: upstreamRef });

    console.log(
        `\nSynced ${files.length} schema files from ${upstreamRepo}@${upstreamRef} -> ${targetDir}` +
            (changed.length > 0 ? `（${changed.length} 个有变化）` : "（全部未变化）"),
    );
}

async function writeManifest(files, source) {
    const manifest = {
        schemaVersion: 1,
        source,
        files: files.map(({ path, url, sha256: hash }) => ({
            path: `${targetDir}/${path}`,
            upstreamPath: `tools/${path}`,
            url: url ?? null,
            sha256: hash,
        })),
    };
    await writeFile(
        join(targetDir, "schema-manifest.json"),
        `${JSON.stringify(manifest, null, 4)}\n`,
        "utf8",
    );
}

async function readFileOrNull(path) {
    try {
        return await readFile(path, "utf8");
    } catch {
        return null;
    }
}

// 取一个 schema 的原文。先走 contents API（返回 base64，本机对这种链路的连通性明显好于
// raw.githubusercontent.com），失败再回落 raw URL。
async function fetchSchemaText(path) {
    const apiUrl = `https://api.github.com/repos/${upstreamRepo}/contents/tools/${path}?ref=${upstreamRef}`;
    try {
        const json = await fetchJsonWithRetry(apiUrl, path);
        if (json.encoding !== "base64" || typeof json.content !== "string") {
            throw new Error(`contents API 返回了非 base64 内容（encoding=${json.encoding}）`);
        }
        return Buffer.from(json.content, "base64").toString("utf8");
    } catch (apiError) {
        console.warn(`contents API 失败，回落 raw：${path}（${apiError.message}）`);
        return fetchTextWithRetry(`${baseUrl}/${path}`, path);
    }
}

async function fetchJsonWithRetry(url, label, attempts = 3) {
    return JSON.parse(await fetchWithRetry(url, label, attempts));
}

async function fetchTextWithRetry(url, label, attempts = 3) {
    return fetchWithRetry(url, label, attempts);
}

// fetch 带重试：这类网络下偶发 ECONNRESET，一次失败就判整体失败会让维护动作变得不可用。
async function fetchWithRetry(url, label, attempts = 3) {
    for (let attempt = 1; ; attempt++) {
        try {
            const response = await fetch(url, {
                signal: AbortSignal.timeout(60_000),
                headers: { "User-Agent": "MaaAshArms-sync-schema", Accept: "application/vnd.github+json" },
            });
            if (!response.ok) {
                throw new Error(`${response.status} ${response.statusText}`);
            }
            return await response.text();
        } catch (error) {
            if (attempt >= attempts) {
                throw new Error(
                    `拉取失败：ref=${upstreamRef} ${label} -> ${error.message}\n` +
                        `URL: ${url}\n` +
                        "确认 ref 是否存在（例如 main / v5.12.2）。\n" +
                        "网络不通时设置 HTTP_PROXY / HTTPS_PROXY 后重试。",
                );
            }
            console.warn(`重试 ${attempt}/${attempts - 1} ${label}：${error.message}`);
            await new Promise((resolve) => setTimeout(resolve, 2000 * attempt));
        }
    }
}

function sha256(content) {
    return createHash("sha256").update(content).digest("hex");
}

function assertRecord(value, label) {
    if (typeof value !== "object" || value === null || Array.isArray(value)) {
        throw new Error(`${label} 必须是对象，实际是 ${JSON.stringify(value)}`);
    }
}

function assertEqual(actual, expected, label) {
    if (actual !== expected) {
        throw new Error(`${label} 必须是 ${JSON.stringify(expected)}，实际是 ${JSON.stringify(actual)}`);
    }
}

function assertRequired(required, field, label) {
    if (!Array.isArray(required) || !required.includes(field)) {
        throw new Error(`${label} 的 required 必须包含 ${field}`);
    }
}
