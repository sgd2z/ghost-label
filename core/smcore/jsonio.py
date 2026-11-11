"""Mechanical JSON handling for model output: extract, repair, validate, coerce.

Schemas are a small JSON Schema subset: type (object/array/string/integer/number/boolean),
properties, required, items, enum, minItems, maxItems. No judgment lives here: values are only
checked for shape, never for meaning.
"""

from __future__ import annotations

import json
import re

THINK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.S | re.I)
OPEN_THINK = re.compile(r"^\s*<(think|thinking|reasoning)>.*?(?=[{\[]\s*\")", re.S | re.I)  # never closed
FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)


def candidates(text: str):
    """Possible JSON documents inside a model reply, most likely first."""
    text = THINK.sub("", text or "").strip()
    text = OPEN_THINK.sub("", text).strip()
    yield text
    for block in FENCE.findall(text):
        yield block.strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        while start != -1:
            end = matching(text, start, opener, closer)
            if end != -1:
                yield text[start:end + 1]
                break
            start = text.find(opener, start + 1)


def matching(text: str, start: int, opener: str, closer: str) -> int:
    depth, in_string, escape = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return i
    return -1


def repair(text: str) -> str:
    """Common syntax slips: trailing commas, smart quotes around keys, Python literals."""
    out = re.sub(r",\s*([}\]])", r"\1", text)
    out = out.replace("\u201c", '"').replace("\u201d", '"')
    out = re.sub(r"(?<![\w\"])True(?![\w\"])", "true", out)
    out = re.sub(r"(?<![\w\"])False(?![\w\"])", "false", out)
    out = re.sub(r"(?<![\w\"])None(?![\w\"])", "null", out)
    return out


def _scan(body):
    stack, in_string, escape, commas = [], False, False, []
    for i, c in enumerate(body):
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c in "{[":
            stack.append("}" if c == "{" else "]")
        elif c in "}]" and stack:
            stack.pop()
        elif c == ",":
            commas.append(i)
    return stack, in_string, commas


def close_truncated(text: str) -> str | None:
    """A reply cut off mid-document (token limit, small model): close the open strings, arrays and
    objects; if the cut fell inside a key or value, drop that last partial item. Mechanical."""
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        return None
    body = text[min(starts):].rstrip()
    stack, in_string, commas = _scan(body)
    if not stack and not in_string:
        return None
    for cut in [len(body)] + list(reversed(commas)):
        seg = body[:cut].rstrip()
        st, ins, _ = _scan(seg)
        seg = seg + ('"' if ins else "")
        seg = re.sub(r"[,:]\s*$", "", seg)
        attempt = seg + "".join(reversed(st))
        try:
            json.loads(repair(attempt))
            return attempt
        except ValueError:
            continue
    return None


def parse(text: str):
    """Returns the first candidate that parses, or raises ValueError."""
    errors = []
    cands = list(candidates(text))
    closed = close_truncated(cands[0]) if cands else None
    ordered = cands[:1] + ([closed] if closed else []) + cands[1:]
    for candidate in ordered:
        for attempt in (candidate, repair(candidate)):
            try:
                return json.loads(attempt)
            except ValueError as error:
                errors.append(str(error))
    raise ValueError("no JSON found in the reply" + (f" ({errors[-1]})" if errors else ""))


TYPES = {"object": dict, "array": list, "string": str, "boolean": bool}


def coerce(value, schema: dict, path: str = "$", errors: list | None = None):
    """Shape `value` toward `schema`. Returns (value, errors). Only lossless or trivial fixes:
    numbers <-> numeric strings, a lone item wrapped in a list, missing optional fields left out."""
    errors = [] if errors is None else errors
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), None) if value is not None else None
        if value is None:
            return None, errors
    if kind == "object":
        required = schema.get("required", [])
        if isinstance(value, str) and len(required) == 1 and \
                schema.get("properties", {}).get(required[0], {}).get("type") == "string":
            value = {required[0]: value}      # "Slow" where {"label": "Slow"} was asked
        if not isinstance(value, dict):
            errors.append(f"{path}: expected an object")
            return value, errors
        props = schema.get("properties", {})
        out = dict(value)
        for key, sub in props.items():
            if key in out:
                out[key], _ = coerce(out[key], sub, f"{path}.{key}", errors)
        for key in schema.get("required", []):
            if key not in out or out[key] is None:
                errors.append(f"{path}.{key}: missing")
        return out, errors
    if kind == "array":
        if isinstance(value, dict) and len(value) == 1 and isinstance(next(iter(value.values())), list):
            value = next(iter(value.values()))
        if not isinstance(value, list):
            value = [value]
        items = schema.get("items")
        out = [coerce(v, items, f"{path}[{i}]", errors)[0] for i, v in enumerate(value)] if items else value
        if len(out) < schema.get("minItems", 0):
            errors.append(f"{path}: needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(out) > schema["maxItems"]:
            out = out[: schema["maxItems"]]
        return out, errors
    if kind == "string":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        elif isinstance(value, list) and all(isinstance(v, str) for v in value):
            value = "\n".join(value)
        if not isinstance(value, str):
            errors.append(f"{path}: expected a string")
        elif "enum" in schema and value not in schema["enum"]:
            lowered = {e.lower(): e for e in schema["enum"]}
            if value.lower() in lowered:
                value = lowered[value.lower()]
            else:
                errors.append(f"{path}: must be one of {schema['enum']}")
        return value, errors
    if kind in ("integer", "number"):
        if isinstance(value, str):
            match = re.search(r"-?\d+(\.\d+)?", value)
            if match:
                value = float(match.group(0))
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"{path}: expected a number")
            return value, errors
        return (int(round(value)) if kind == "integer" else float(value)), errors
    if kind == "boolean":
        if isinstance(value, str) and value.lower() in ("true", "false", "yes", "no"):
            value = value.lower() in ("true", "yes")
        if not isinstance(value, bool):
            errors.append(f"{path}: expected true or false")
        return value, errors
    return value, errors


def strict_schema(schema: dict) -> dict:
    """The same schema with additionalProperties: false, for providers' json_schema mode."""
    out = dict(schema)
    if out.get("type") == "object":
        out["properties"] = {k: strict_schema(v) for k, v in out.get("properties", {}).items()}
        out.setdefault("additionalProperties", False)
    if out.get("type") == "array" and isinstance(out.get("items"), dict):
        out["items"] = strict_schema(out["items"])
    out.pop("maxItems", None)
    return out


def describe(schema: dict, indent: int = 0) -> str:
    """A compact human-readable shape for prompts (works with providers that lack json_schema)."""
    pad = "  " * indent
    kind = schema.get("type")
    note = f"  // {schema['description']}" if schema.get("description") else ""
    if kind == "object":
        required = set(schema.get("required", []))
        lines = ["{" + note]
        for key, sub in schema.get("properties", {}).items():
            optional = "" if key in required else " (optional)"
            lines.append(f"{pad}  \"{key}\"{optional}: " + describe(sub, indent + 1).lstrip())
        lines.append(pad + "}")
        return "\n".join(lines)
    if kind == "array":
        return "[ " + describe(schema.get("items", {}), indent).lstrip() + " , ... ]" + note
    if "enum" in schema:
        return " | ".join(json.dumps(e) for e in schema["enum"]) + note
    if isinstance(kind, list):
        kind = " or ".join(kind)
    return f"<{kind}>" + note
