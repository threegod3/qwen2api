"""JSON Schema 压缩：把冗长的 JSON Schema 转成紧凑的 TypeScript-like 签名。

动机：
    一个完整 JSON Schema 工具定义（含 description、type、required、enum 等）单个约
    1.5KB。90 个工具合计 ~135KB prompt。
    压缩成 TS 签名后，单个工具约 150~250 bytes。90 个工具 ~15KB。**省 10 倍空间**。
    输入越小，上游输出预算越多，截断率越低。

示例：
    输入 schema:
        {"type":"object","properties":{
            "file_path":{"type":"string","description":"..."},
            "encoding":{"type":"string","enum":["utf-8","base64"]}
         },
         "required":["file_path"]}

    输出 TS 签名:
        {file_path!: string, encoding?: utf-8|base64}

    `!` = required, `?` = optional
"""

from __future__ import annotations

from typing import Any


def _type_of(prop: dict[str, Any]) -> str:
    """把单个 property 的类型压缩成简短字符串。"""
    if not isinstance(prop, dict):
        return "any"

    # enum：直接列值
    if "enum" in prop and isinstance(prop["enum"], list) and prop["enum"]:
        vals = []
        for v in prop["enum"]:
            if isinstance(v, str):
                vals.append(v)
            else:
                vals.append(str(v))
        return "|".join(vals)

    base_type = prop.get("type", "any")

    # array：标注 item 类型
    if base_type == "array":
        items = prop.get("items")
        if isinstance(items, dict):
            item_type = _type_of(items)
            return f"{item_type}[]"
        return "any[]"

    # object：嵌套对象 → 递归压缩
    if base_type == "object" and isinstance(prop.get("properties"), dict):
        return compact_schema(prop)

    # union type（type 是 list）
    if isinstance(base_type, list):
        return "|".join(str(t) for t in base_type)

    return str(base_type) if base_type else "any"


def compact_schema(schema: dict[str, Any]) -> str:
    """压缩一个 JSON Schema 为 TS-like 签名。

    返回示例：`{file_path!: string, encoding?: utf-8|base64}`
    """
    if not isinstance(schema, dict):
        return "{}"
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return "{}"
    required = set(schema.get("required", []) if isinstance(schema.get("required"), list) else [])
    parts = []
    for name, spec in props.items():
        type_str = _type_of(spec if isinstance(spec, dict) else {})
        marker = "!" if name in required else "?"
        parts.append(f"{name}{marker}: {type_str}")
    return "{" + ", ".join(parts) + "}"


def render_tool_signature(tool: dict[str, Any], desc_max_len: int = 50) -> str:
    """渲染单个工具为紧凑一行："- name: desc\n  Params: {...}"。"""
    name = tool.get("name", "")
    desc = (tool.get("description", "") or "").strip()
    if desc_max_len > 0 and len(desc) > desc_max_len:
        desc = desc[:desc_max_len] + "…"
    schema = tool.get("parameters") or tool.get("input_schema") or {}
    sig = compact_schema(schema)
    line = f"- {name}"
    if desc:
        line += f": {desc}"
    if sig and sig != "{}":
        line += f"\n  Params: {sig}"
    return line
