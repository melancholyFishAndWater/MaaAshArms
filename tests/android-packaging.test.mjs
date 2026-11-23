// SPDX-License-Identifier: AGPL-3.0-or-later
// Tests for the AGPL-derived Android packaging resolver; no network is used.
import test from "node:test";
import assert from "node:assert/strict";
import {mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync} from "node:fs";
import {tmpdir} from "node:os";
import {join, resolve} from "node:path";
import {
    CORE_PROVIDES, compareMaafwVersions, maafwVersionKey, pickPackagingCore,
    parseRequirementPin, requirementEntries, indexVersions, chaquopyPins,
    resolvePackaging, artifactPrefix, agentDeclared, main,
} from "../tools/android-packaging.mjs";

const variables = ["ANDROID_PACKAGING_RELEASES_FIXTURE", "ANDROID_PACKAGING_CHAQUOPY_FIXTURE",
    "ANDROID_PACKAGING_ALLOW_FALLBACK", "AGENT_CORE_TAG", "GITHUB_OUTPUT"];
function fixture(t, {pin = "5.15.0", cores = ["3.13.15-maafw5.15.0"], wheels = {}, agent = {child_exec: "uv"}} = {}) {
    const root = mkdtempSync(join(tmpdir(), "maa-android-test-"));
    const saved = new Map(variables.map((key) => [key, process.env[key]]));
    for (const key of variables) delete process.env[key];
    const originalFetch = globalThis.fetch;
    globalThis.fetch = () => { throw new Error("Unexpected network in offline test"); };
    t.after(() => {
        globalThis.fetch = originalFetch;
        for (const [key, value] of saved) {
            if (value === undefined) delete process.env[key]; else process.env[key] = value;
        }
        rmSync(root, {recursive: true, force: true});
    });
    mkdirSync(join(root, "assets"));
    writeFileSync(join(root, "assets/interface.json"), JSON.stringify({name: "MaaAshArms", agent}));
    writeFileSync(join(root, "requirements.txt"), `maafw==${pin}\nnumpy==2.5.3\nstrenum==0.4.15\n`);
    const releases = join(root, "releases.json"), packages = join(root, "wheels.json");
    writeFileSync(releases, JSON.stringify({core: cores, maafw: []}));
    writeFileSync(packages, JSON.stringify(wheels));
    process.env.ANDROID_PACKAGING_RELEASES_FIXTURE = releases;
    process.env.ANDROID_PACKAGING_CHAQUOPY_FIXTURE = packages;
    return {root, releases, packages};
}

test("version aliases normalize PEP440 and GitHub prereleases", () => {
    assert.equal(maafwVersionKey("v5.15.0-beta.10"), "5.15.0b10");
    assert.equal(compareMaafwVersions("5.15.0b10", "v5.15.0-beta.10"), 0);
    const versions = ["5.15.0", "5.15.0rc2", "5.15.0b10", "5.15.0b2", "5.15.0a1", "5.14.10"];
    assert.deepEqual(versions.sort(compareMaafwVersions), ["5.14.10", "5.15.0a1", "5.15.0b2", "5.15.0b10", "5.15.0rc2", "5.15.0"]);
});

test("exact core match selects highest CPython and preserves real tag spelling", () => {
    const tags = ["3.13.9-maafw5.15.0b1", "3.13.15-maafw5.15.0-beta.1", "3.13.20-maafw5.14.0"];
    assert.deepEqual(pickPackagingCore(tags, "5.15.0b1"), {
        coreTag: "3.13.15-maafw5.15.0-beta.1", maafwVersion: "5.15.0-beta.1", degraded: false,
    });
});

test("missing exact pairing fails unless fallback explicitly enabled", () => {
    const tags = ["3.13.15-maafw5.14.9", "3.13.15-maafw5.14.10", "3.13.15-maafw5.16.0"];
    assert.throws(() => pickPackagingCore(tags, "5.15.0"), /No exact/);
    assert.equal(pickPackagingCore(tags, "5.15.0", true).maafwVersion, "5.14.10");
    assert.throws(() => pickPackagingCore(tags, "5.13.0", true), /没有不高于/);
});

test("fallback never upgrades a prerelease to stable or a later prerelease", () => {
    const tags = ["3.13.15-maafw5.15.0", "3.13.15-maafw5.15.0b10", "3.13.15-maafw5.15.0b1"];
    assert.equal(pickPackagingCore(tags, "5.15.0b2", true).maafwVersion, "5.15.0b1");
});

test("requirements is the only version source and rejects ambiguous pins", () => {
    assert.equal(parseRequirementPin("# header\r\nmaafw==5.15.0\r\n    # via project"), "5.15.0");
    for (const value of [undefined, "numpy==2", "maafw>=5.15.0", "maafw==5.15.*", "maafw==5.15.0; sys_platform=='linux'", "maafw==5.15.0\nmaafw==5.14.0"]) {
        assert.throws(() => parseRequirementPin(value));
    }
});

test("interface prefix and both PI agent declaration forms", () => {
    assert.equal(artifactPrefix({name: "MaaAshArms"}), "MaaAshArms");
    for (const name of [undefined, "../../file", "a\nb", "中文", "a b"]) assert.throws(() => artifactPrefix({name}));
    for (const agent of [{child_exec: "uv"}, [{}]]) assert.equal(agentDeclared({agent}), true);
    for (const agent of [undefined, null, []]) assert.equal(agentDeclared({agent}), false);
});

test("exact offline resolution works without maa-project.json", async (t) => {
    const {root} = fixture(t);
    // A contradictory project file must have no effect in this repository.
    writeFileSync(join(root, "maa-project.json"), JSON.stringify({maafw: {version: "9.0.0"}, project: {displayName: "Wrong"}}));
    const result = await resolvePackaging(root);
    assert.equal(result.artifactPrefix, "MaaAshArms");
    assert.equal(result.requirementPin, "5.15.0");
    assert.equal(result.maafwTag, "v5.15.0");
    assert.equal(result.coreTag, "3.13.15-maafw5.15.0");
    assert.equal(result.coreFallback, "");
    assert.equal(result.hasAgent, true);
    assert.deepEqual(result.warnings, []);
});

test("fallback environment opt-in keeps native library and core versions consistent", async (t) => {
    const {root} = fixture(t, {cores: ["3.13.15-maafw5.14.3"]});
    await assert.rejects(resolvePackaging(root), /No exact/);
    process.env.ANDROID_PACKAGING_ALLOW_FALLBACK = "1";
    const result = await resolvePackaging(root);
    assert.equal(result.maafwTag, "v5.14.3");
    assert.match(result.coreTag, /maafw5.14.3$/);
    assert.match(result.coreFallback, /5.15.0.*5.14.3/);
    assert.equal(result.warnings.length, 1);
});

test("AGENT_CORE_TAG cannot silently override the requirements source", async (t) => {
    const {root} = fixture(t);
    process.env.AGENT_CORE_TAG = "3.13.15-maafw5.14.3";
    await assert.rejects(resolvePackaging(root), /must match/);
    const result = await resolvePackaging(root, {allowFallback: true});
    assert.equal(result.maafwTag, "v5.14.3");
    process.env.AGENT_CORE_TAG = "3.13.15-maafw5.16.0";
    await assert.rejects(resolvePackaging(root, {allowFallback: true}), /must match/);
});

test("prerelease download uses actual release tag, not guessed PEP440 spelling", async (t) => {
    const {root, releases} = fixture(t, {pin: "5.15.0b2", cores: ["3.13.15-maafw5.15.0-beta.2"]});
    writeFileSync(releases, JSON.stringify({core: ["3.13.15-maafw5.15.0-beta.2"], maafw: [{tag: "v5.15.0-beta.2", prerelease: true}]}));
    assert.equal((await resolvePackaging(root)).maafwTag, "v5.15.0-beta.2");
    writeFileSync(releases, JSON.stringify({core: ["3.13.15-maafw5.15.0-beta.2"], maafw: []}));
    await assert.rejects(resolvePackaging(root), /release tag not found/);
});

test("CORE_PROVIDES bypasses Chaquopy entirely", async (t) => {
    fixture(t);
    delete process.env.ANDROID_PACKAGING_CHAQUOPY_FIXTURE;
    assert.deepEqual([...CORE_PROVIDES], ["maafw", "numpy", "strenum"]);
    assert.equal(await chaquopyPins("https://invalid/", [...CORE_PROVIDES].map((name) => ({name, version: "999"})), "cp313"), "");
});

test("requirements normalizes package names and excludes conditional platform pins", () => {
    assert.deepEqual(requirementEntries("Pillow==13.0.0\nfoo_bar==1.0\ncolorama==1; sys_platform=='win32'\n # via x\n--hash=abc"), [
        {name: "pillow", version: "13.0.0"}, {name: "foo-bar", version: "1.0"},
    ]);
});

test("Chaquopy keeps exact compatible version or chooses highest compatible numeric version", async (t) => {
    fixture(t, {wheels: {pillow: ["pillow-9.9.0-0-cp313-cp313-android_24_arm64_v8a.whl", "pillow-11.0.0-0-cp313-cp313-android_24_arm64_v8a.whl", "pillow-12.0.0-0-cp312-cp312-android_24_arm64_v8a.whl"]}});
    assert.equal(await chaquopyPins("https://invalid/", [{name: "pillow", version: "11.0.0"}], "cp313"), "");
    assert.equal(await chaquopyPins("https://invalid/", [{name: "pillow", version: "13.0.0"}], "cp313"), "--exclude pillow --require pillow==11.0.0");
    assert.equal(await chaquopyPins("https://invalid/", [{name: "pure-package", version: "1.0"}], "cp313"), "");
});

test("Chaquopy fixture changes do not leak cached packages", async (t) => {
    const {packages} = fixture(t, {wheels: {pillow: ["pillow-11.0.0-cp313-cp313-android.whl"]}});
    assert.match(await chaquopyPins("https://invalid/", [{name: "pillow", version: "13.0.0"}], "cp313"), /11.0.0/);
    writeFileSync(packages, JSON.stringify({pillow: ["pillow-10.0.0-cp313-cp313-android.whl"]}));
    assert.match(await chaquopyPins("https://invalid/", [{name: "pillow", version: "13.0.0"}], "cp313"), /10.0.0/);
});

test("Chaquopy rejects native package without matching CPython", async (t) => {
    fixture(t, {wheels: {pillow: ["pillow-11.0.0-cp312-cp312-android.whl"]}});
    await assert.rejects(chaquopyPins("https://invalid/", [{name: "pillow", version: "13.0.0"}], "cp313"), /没有 cp313/);
    assert.deepEqual([...indexVersions(["x-1.0-py3-none-any.whl", "x-2.0-py2.py3-none-any.whl", "wrong-3.0-cp313-none-any.whl"], "x", "cp313")], ["1.0", "2.0"]);
});

test("CLI help is offline and outputs are consistent with requirements", async (t) => {
    const {root} = fixture(t);
    assert.equal(await main(["--help"]), 0);
    const output = join(root, "outputs.txt");
    process.env.GITHUB_OUTPUT = output;
    assert.equal(await main([root]), 0);
    const values = Object.fromEntries(readFileSync(output, "utf8").trimEnd().split("\n").map((line) => {
        const i = line.indexOf("="); return [line.slice(0, i), line.slice(i + 1)];
    }));
    assert.equal(values.maafw_tag, "v5.15.0");
    assert.equal(values.core_tag, "3.13.15-maafw5.15.0");
    assert.equal(values.artifact_prefix, "MaaAshArms");
    assert.equal(values.core_fallback, "");
    assert.equal(await main([root, "--unknown"]), 1);
});

test("actual repository version and artifact name resolve using offline releases", async (t) => {
    fixture(t);
    const result = await resolvePackaging(resolve(import.meta.dirname, ".."));
    assert.equal(result.requirementPin, "5.15.0");
    assert.equal(result.artifactPrefix, "MaaAshArms");
});
