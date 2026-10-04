// 校验工作区里改动的 PNG 是否只是"压缩流变化"：IHDR 不变 + **未过滤（解压后还原 PNG 过滤
// 器）的像素**逐字节相同 + PLTE/tRNS 不变。
//
// 注意别只比 IDAT：oxipng 会重新选择每行的过滤器（filter 类型 1/2/3/4），所以 IDAT 解压出来
// 的原始字节本来就允许不同；只有走完 unfilter 之后的像素值才是"图本身"。
//
// 用途：跑完 `npm run optimize:images` 之后确认无损性。要对比 HEAD 里的版本，先用
// PowerShell 把旧文件导到 tools/.png-old/（Node 在受限沙箱里不能用管道 spawn 子进程）：
//
//   $old = 'tools\.png-old'
//   Remove-Item -Recurse -Force $old -ErrorAction SilentlyContinue
//   New-Item -ItemType Directory -Force -Path $old | Out-Null
//   git status --porcelain | Where-Object { $_ -match '\.png$' } | ForEach-Object {
//       $f = $_.Substring(3).Trim()
//       $out = (Join-Path (Get-Location) (Join-Path $old ($f -replace '/', '__'))) -replace '\\', '/'
//       cmd /c "git show HEAD:`"$f`" > `"$out`""
//   }
//   node tools/check-png-lossless.mjs
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { inflateSync } from "node:zlib";

const OLD_ROOT = "tools/.png-old";

if (!existsSync(OLD_ROOT)) {
    console.error(
        `找不到 ${OLD_ROOT}/。请先按文件头部注释里的 PowerShell 片段把 HEAD 版本导出，再运行本脚本。`,
    );
    process.exit(1);
}

function parsePng(buffer) {
    let i = 8;
    let ihdr = null;
    const idat = [];
    let plte = null;
    let trns = null;
    while (i + 12 <= buffer.length) {
        const length = buffer.readUInt32BE(i);
        const type = buffer.toString("latin1", i + 4, i + 8);
        const data = buffer.subarray(i + 8, i + 8 + length);
        if (type === "IHDR") {
            ihdr = {
                width: data.readUInt32BE(0),
                height: data.readUInt32BE(4),
                bitDepth: data[8],
                colorType: data[9],
                compression: data[10],
                filter: data[11],
                interlace: data[12],
            };
        } else if (type === "IDAT") idat.push(data);
        else if (type === "PLTE") plte = data;
        else if (type === "tRNS") trns = data;
        i += 12 + length;
    }
    return { ihdr, pixels: inflateSync(Buffer.concat(idat)), plte, trns };
}

function paeth(a, b, c) {
    const p = a + b - c;
    const pa = Math.abs(p - a);
    const pb = Math.abs(p - b);
    const pc = Math.abs(p - c);
    return pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
}

// 还原 PNG 的逐行过滤器，得到真正的像素字节。oxipng 会换过滤策略，所以必须走这一步。
function unfilter(png) {
    const { ihdr, pixels } = png;
    if (ihdr.interlace !== 0) throw new Error("不支持交错 PNG");
    const channels = { 0: 1, 2: 3, 3: 1, 4: 2, 6: 4 }[ihdr.colorType];
    if (!channels) throw new Error(`不支持的 colorType ${ihdr.colorType}`);
    if (ihdr.bitDepth !== 8) throw new Error(`不支持的 bitDepth ${ihdr.bitDepth}`);

    const stride = ihdr.width * channels;
    const out = Buffer.alloc(stride * ihdr.height);
    let pos = 0;
    for (let y = 0; y < ihdr.height; y++) {
        const filter = pixels[pos++];
        const line = pixels.subarray(pos, pos + stride);
        pos += stride;
        const dest = out.subarray(y * stride, (y + 1) * stride);
        const prev = y > 0 ? out.subarray((y - 1) * stride, y * stride) : Buffer.alloc(stride);
        for (let x = 0; x < stride; x++) {
            const a = x >= channels ? dest[x - channels] : 0;
            const b = prev[x];
            const c = x >= channels ? prev[x - channels] : 0;
            const v = line[x];
            dest[x] =
                filter === 0 ? v
                : filter === 1 ? (v + a) & 0xff
                : filter === 2 ? (v + b) & 0xff
                : filter === 3 ? (v + ((a + b) >> 1)) & 0xff
                : (v + paeth(a, b, c)) & 0xff;
        }
    }
    return out;
}

const failures = [];
const entries = readdirSync(OLD_ROOT);

for (const entry of entries) {
    const file = entry.replaceAll("__", "/");
    let oldPng;
    let newPng;
    try {
        oldPng = parsePng(readFileSync(`${OLD_ROOT}/${entry}`));
        newPng = parsePng(readFileSync(file));
    } catch (error) {
        failures.push(`${file}: 解析失败 ${error.message}`);
        continue;
    }

    if (JSON.stringify(oldPng.ihdr) !== JSON.stringify(newPng.ihdr)) {
        failures.push(
            `${file}: IHDR 变了 ${JSON.stringify(oldPng.ihdr)} -> ${JSON.stringify(newPng.ihdr)}` +
                "（多半是 oxipng 做了颜色空间精简，检查是否漏了 --nc/--np/--ng）",
        );
        continue;
    }
    let oldPixels;
    let newPixels;
    try {
        oldPixels = unfilter(oldPng);
        newPixels = unfilter(newPng);
    } catch (error) {
        failures.push(`${file}: 解码失败 ${error.message}`);
        continue;
    }
    if (!oldPixels.equals(newPixels)) {
        let first = -1;
        let count = 0;
        for (let k = 0; k < Math.min(oldPixels.length, newPixels.length); k++) {
            if (oldPixels[k] !== newPixels[k]) {
                if (first < 0) first = k;
                count++;
            }
        }
        failures.push(
            `${file}: 像素数据不同（${count} 字节，首个 offset=${first}）`,
        );
        continue;
    }
    for (const key of ["plte", "trns"]) {
        const a = oldPng[key] ?? Buffer.alloc(0);
        const b = newPng[key] ?? Buffer.alloc(0);
        if (!a.equals(b)) failures.push(`${file}: ${key.toUpperCase()} 块不同`);
    }
}

console.log(`checked ${entries.length} file(s)`);
if (failures.length > 0) {
    console.log(failures.join("\n"));
    process.exit(1);
}
console.log("OK: 全部文件的 IHDR、未过滤像素与调色板逐字节一致");
