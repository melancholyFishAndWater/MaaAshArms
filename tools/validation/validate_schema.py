#!/usr/bin/env python3
import argparse
import json
import sys
import tempfile
from importlib.util import find_spec
from pathlib import Path

from jsonschema import Draft7Validator, Draft202012Validator

# referencing 是可选依赖：没装就退回 jsonschema 自带的 RefResolver。
# 用 find_spec 探测而不是 `import referencing.retrieval`——后者只为可用性判定而 import，
# 读代码的人会以为 retrieval 被用到了（ruff F401 也会报它）。
HAS_REFERENCING = find_spec("referencing") is not None

# 资源目录根部的默认值文件：形状不是节点定义，不能按 pipeline.schema.json 校验
DEFAULT_PIPELINE_NAME = "default_pipeline.json"
DEFAULT_PIPELINE_SCHEMA = "tools/vscode/default_pipeline.schema.json"
# 资源目录顶层的 config/ 放的是 **MaaCore 的运行时选项**（`maa_option.json`：draw_quality / logging /
# save_draw / save_on_error / stdout_level，标量键值，不是节点定义），而且该目录在 .gitignore 里
# （`.gitignore` 的 `config` 一行）——CI 干净检出时根本不存在。按 pipeline schema 校验它只会在本机
# 报 5 条"is not of type 'object'"的假 error，所以按运行时文件跳过。
RUNTIME_OPTION_DIRS = frozenset({"config"})

if HAS_REFERENCING:
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT7, DRAFT202012


def strip_jsonc_comments(text):
    """
    移除 JSONC 注释，保持 JSON 结构完整
    """
    # 状态机：0=正常, 1=字符串中, 2=转义字符
    result = []
    state = 0
    i = 0

    while i < len(text):
        char = text[i]

        if state == 0:  # 正常状态
            if char == '"':
                result.append(char)
                state = 1
                i += 1
            elif i + 1 < len(text) and text[i : i + 2] == "//":
                # 单行注释，跳到行尾
                while i < len(text) and text[i] != "\n":
                    i += 1
                if i < len(text):
                    result.append("\n")  # 保留换行
                    i += 1
            elif i + 1 < len(text) and text[i : i + 2] == "/*":
                # 多行注释，跳到 */
                i += 2
                while i + 1 < len(text) and text[i : i + 2] != "*/":
                    if text[i] == "\n":
                        result.append("\n")  # 保留换行以维持行号
                    i += 1
                i += 2  # 跳过 */
            else:
                result.append(char)
                i += 1
        elif state == 1:  # 字符串中
            result.append(char)
            if char == "\\":
                state = 2
            elif char == '"':
                state = 0
            i += 1
        elif state == 2:  # 转义字符
            result.append(char)
            state = 1
            i += 1

    return "".join(result)


def load_jsonc(file_path):
    """加载 JSONC 文件"""
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 移除注释
    clean_content = strip_jsonc_comments(content)

    try:
        return json.loads(clean_content)
    except json.JSONDecodeError as e:
        print(f"JSON decode error in {file_path}: {e}")
        # 调试：保存清理后的内容
        debug_file = Path(tempfile.gettempdir()) / f"debug_{Path(file_path).name}"
        with open(debug_file, "w") as f:
            f.write(clean_content)
        print(f"Cleaned content saved to {debug_file}")
        raise


def get_validator_class(schema):
    """根据 schema 的 $schema 字段选择合适的验证器"""
    schema_uri = schema.get("$schema", "")

    if "draft-07" in schema_uri or "draft/07" in schema_uri:
        return Draft7Validator
    elif "2020-12" in schema_uri:
        return Draft202012Validator
    else:
        # 默认使用 2020-12
        return Draft202012Validator


def find_line_number(file_path, json_path):
    """在文件中查找JSON路径对应的行号

    为了避免找到错误的子字段，只返回顶层对象的行号
    例如：/NoSmallGlobe/recognition -> 返回 NoSmallGlobe 的行号
    """
    if not json_path or json_path == "/":
        return None

    parts = [p for p in json_path.split("/") if p]
    if not parts:
        return None

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        # 只查找第一层（顶层对象）
        # 确保找到的是键名（后面跟着冒号），而不是注释或字符串值
        key = parts[0]
        import re

        # 匹配 "key": 或 "key" : 的模式
        pattern = re.compile(rf'"{re.escape(key)}"\s*:')

        for i, line in enumerate(lines):
            if pattern.search(line):
                return i + 1  # 行号从1开始

    except (OSError, UnicodeDecodeError) as exc:
        # 不裸吞：读不了文件时要留一句，否则"报错没有行号"会被当成 JSON 结构问题。
        print(f"[提示] 找不到 {file_path} 里 \"{'.'.join(parts)}\" 的行号：{type(exc).__name__}: {exc}",
              file=sys.stderr)

    return None


def validate_file(file_path, validator):
    """验证单个文件"""
    try:
        data = load_jsonc(file_path)
        errors = list(validator.iter_errors(data))

        if errors:
            print(f"\n❌ Validation failed for {file_path}:")
            print(f"   Found {len(errors)} error(s):")
            for idx, error in enumerate(errors[:10], 1):
                path = "/" + "/".join(str(p) for p in error.path) if error.path else "/"
                # print(f"   {idx}. {path}: {error.message}")

                # 尝试找到行号并输出GitHub Actions格式的错误注解
                line_num = find_line_number(file_path, path)
                if line_num:
                    print(
                        f"::error file={file_path},line={line_num},title=Schema Validation Error::{path}: {error.message}"
                    )
                else:
                    print(
                        f"::error file={file_path},title=Schema Validation Error::{path}: {error.message}"
                    )
            return False

        print(f"✓ {file_path}")
        return True
    except Exception as e:  # noqa: BLE001 - schema 校验可能抛很多种异常，这里统一报成一条校验失败并继续跑下一个文件
        print(f"\n❌ Error validating {file_path}: {e}")
        # 输出GitHub Actions格式的错误注解
        print(f"::error file={file_path},title=Validation Error::{e}")
        return False


def create_validator(schema, schema_store):
    """创建 validator，使用新的 referencing API 或回退到 RefResolver。

    **不把 `get_validator_class()` 的结果存进变量再调用**：它的返回类型是
    `type[Draft7Validator] | type[Draft202012Validator]`，pyright 会按联合类型求交去核对
    `__init__`，然后报一串 "Argument missing for parameter schema" / "No parameter named
    registry"（Pylance 口径实测 7 条，全是假红）。所以这里按 schema 分两支、各自直接实例化，
    让每个调用点的类型都是单一类。
    """
    is_2020 = get_validator_class(schema) is Draft202012Validator

    if HAS_REFERENCING:
        # 使用新的 referencing API
        registry = Registry()

        # 根据 schema 类型选择规范
        spec = DRAFT202012 if is_2020 else DRAFT7

        # 添加所有 schema 到 registry
        for uri, schema_content in schema_store.items():
            resource = Resource.from_contents(
                schema_content, default_specification=spec
            )
            registry = registry.with_resource(uri, resource)

        if is_2020:
            return Draft202012Validator(schema, registry=registry)
        return Draft7Validator(schema, registry=registry)
    else:
        # 回退到旧的 RefResolver。**在分支里 import**：`referencing` 装了的时候上面那个
        # 分支根本不会执行，放模块级会被类型检查判成不可达。
        from jsonschema import RefResolver

        # 从 schema_store 中找到主 schema 的 URI
        schema_uri = None
        for uri, content in schema_store.items():
            if content == schema:
                schema_uri = uri
                break

        if schema_uri is None:
            schema_uri = "file:///schema.json"

        # 这一行挂抑制而不是改写法：`jsonschema.RefResolver` 是 `jsonschema/validators.py`
        # 模块级 `__getattr__` 动态返回的（`elif name == "RefResolver": return _RefResolver`，
        # 只为发 DeprecationWarning），静态类型检查看不到它是类，于是判成"不可调用"并连带
        # 6 条 reportCallIssue（Pylance 口径实测）。也不能改用 `jsonschema.validators._RefResolver`——
        # 没有 jsonschema stub 的环境里那是个"未知导入符号"，仓库自己的 pyright 门槛会报 error。
        # **抑制必须与报错同一行**：拆成多行时 pyright 报在参数那一行，行首挂的抑制压不住。
        resolver = RefResolver(base_uri=schema_uri, referrer=schema, store=schema_store)  # type: ignore
        if is_2020:
            return Draft202012Validator(schema, resolver=resolver)
        return Draft7Validator(schema, resolver=resolver)


def main():
    parser = argparse.ArgumentParser(
        description="Validate JSON/JSONC files against JSON Schema"
    )
    parser.add_argument(
        "--schema-dir",
        type=str,
        default="tools/schema",
        help="Directory containing schema files (default: tools/schema)",
    )
    parser.add_argument(
        "--resource-dirs",
        type=str,
        nargs="+",
        default=["assets/resource"],
        help="Directories containing resource files to validate (default: assets/resource)",
    )
    parser.add_argument(
        "--exclude-dirs",
        type=str,
        nargs="*",
        default=[],
        help="Directories to exclude from pipeline validation (default: none)",
    )
    parser.add_argument(
        "--interface-files",
        type=str,
        nargs="+",
        default=["assets/interface.json"],
        help="Path to interface.json files (default: assets/interface.json)",
    )
    parser.add_argument(
        "--task-dirs",
        type=str,
        nargs="*",
        default=[],
        help="Directories containing task files to validate against interface_import.schema.json (default: none)",
    )
    parser.add_argument(
        "--default-pipeline-schema",
        type=str,
        default=DEFAULT_PIPELINE_SCHEMA,
        help=f"Schema for <resource-dir>/{DEFAULT_PIPELINE_NAME} (default: {DEFAULT_PIPELINE_SCHEMA})",
    )

    args = parser.parse_args()

    all_valid = True

    # 加载所有 schema 文件
    schema_dir = Path(args.schema_dir).resolve()
    schema_store = {}

    print("Loading schemas...")
    for schema_file in schema_dir.glob("*.json"):
        try:
            schema = load_jsonc(schema_file)
            # 使用多种格式的 URI 作为 key
            file_uri = schema_file.as_uri()
            relative_path = f"./{schema_file.name}"
            absolute_path = f"/{schema_file.name}"

            schema_store[file_uri] = schema
            schema_store[relative_path] = schema
            schema_store[absolute_path] = schema
        except Exception as e:  # noqa: BLE001 - 单个 schema 读坏不该让整轮校验停摆，跳过并继续
            print(f"Warning: Failed to load schema {schema_file}: {e}")

    # 加载并创建 pipeline validator
    pipeline_schema_path = schema_dir / "pipeline.schema.json"
    pipeline_schema = load_jsonc(pipeline_schema_path)
    pipeline_schema_uri = pipeline_schema_path.as_uri()
    schema_store[pipeline_schema_uri] = pipeline_schema

    pipeline_validator = create_validator(pipeline_schema, schema_store)

    # 准备排除目录列表
    exclude_paths = [Path(d).resolve() for d in args.exclude_dirs]

    def is_excluded(file_path):
        """检查文件是否在排除目录中"""
        file_path = Path(file_path).resolve()
        for exclude_path in exclude_paths:
            try:
                file_path.relative_to(exclude_path)
                return True
            except ValueError:
                continue
        return False

    def is_runtime_option(file_path, resource_path):
        """资源目录**顶层**的 config/ 是 MaaCore 运行时选项，不是 pipeline 内容（只看顶层，
        避免把任务片段里真叫 config 的子目录也一起放过）。"""
        try:
            rel = Path(file_path).resolve().relative_to(Path(resource_path).resolve())
        except ValueError:
            return False
        return len(rel.parts) > 1 and rel.parts[0] in RUNTIME_OPTION_DIRS

    print("Validating pipeline resources...")
    # 验证 pipeline 资源文件
    for resource_dir in args.resource_dirs:
        resource_path = Path(resource_dir)
        if not resource_path.exists():
            print(
                f"Warning: Resource directory {resource_dir} does not exist, skipping..."
            )
            continue

        for file_path in resource_path.rglob("*.json"):
            if (
                is_excluded(file_path)
                or is_runtime_option(file_path, resource_path)
                or file_path.name == DEFAULT_PIPELINE_NAME
            ):
                continue
            if not validate_file(file_path, pipeline_validator):
                all_valid = False

        for file_path in resource_path.rglob("*.jsonc"):
            if (
                is_excluded(file_path)
                or is_runtime_option(file_path, resource_path)
                or file_path.name == DEFAULT_PIPELINE_NAME
            ):
                continue
            if not validate_file(file_path, pipeline_validator):
                all_valid = False

    # default_pipeline.json 不是节点定义，按它自己的 schema 校验（块名写错会静默不生效）
    default_schema_path = Path(args.default_pipeline_schema)
    default_checked = 0
    if default_schema_path.is_file():
        print("\nValidating default_pipeline.json...")
        default_schema = load_jsonc(default_schema_path)
        default_schema_uri = default_schema_path.resolve().as_uri()
        schema_store[default_schema_uri] = default_schema
        default_validator = create_validator(default_schema, schema_store)
        for resource_dir in args.resource_dirs:
            default_file = Path(resource_dir) / DEFAULT_PIPELINE_NAME
            if not default_file.is_file():
                continue
            default_checked += 1
            if not validate_file(default_file, default_validator):
                all_valid = False
    elif default_checked == 0:
        print(
            f"Warning: default pipeline schema {default_schema_path} does not exist, skipping..."
        )

    print("\nValidating interface files...")
    # 验证 interface 文件
    interface_schema_path = schema_dir / "interface.schema.json"
    if interface_schema_path.exists():
        interface_schema = load_jsonc(interface_schema_path)
        interface_schema_uri = interface_schema_path.as_uri()
        schema_store[interface_schema_uri] = interface_schema

        interface_validator = create_validator(interface_schema, schema_store)

        for interface_file in args.interface_files:
            interface_path = Path(interface_file)
            if interface_path.exists():
                if not validate_file(interface_path, interface_validator):
                    all_valid = False
            else:
                print(
                    f"Warning: Interface file {interface_file} does not exist, skipping..."
                )

    # 验证 task 文件
    if args.task_dirs:
        print("\nValidating task files...")
        task_schema_path = schema_dir / "interface_import.schema.json"
        if task_schema_path.exists():
            task_schema = load_jsonc(task_schema_path)
            task_schema_uri = task_schema_path.as_uri()
            schema_store[task_schema_uri] = task_schema

            task_validator = create_validator(task_schema, schema_store)

            for task_dir in args.task_dirs:
                task_path = Path(task_dir)
                if not task_path.exists():
                    print(
                        f"Warning: Task directory {task_dir} does not exist, skipping..."
                    )
                    continue

                for file_path in task_path.rglob("*.json"):
                    if not validate_file(file_path, task_validator):
                        all_valid = False

                for file_path in task_path.rglob("*.jsonc"):
                    if not validate_file(file_path, task_validator):
                        all_valid = False
        else:
            print(
                f"Warning: Task schema {task_schema_path} does not exist, skipping task validation..."
            )

    if all_valid:
        print("\n✅ All validations passed!")
        sys.exit(0)
    else:
        print("\n❌ Some validations failed!")
        sys.exit(1)


if __name__ == "__main__":
    main()
