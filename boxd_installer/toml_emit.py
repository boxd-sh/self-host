"""Minimal TOML emitter for generated service configs.

Ordering rule (load-bearing: violating it makes services crash-loop on a
misread config): within any table, scalars and plain arrays come first, then
sub-tables, then arrays-of-tables LAST. Otherwise TOML binds later keys into
the last array element.
"""

AOT_KEYS = {"static_records", "internal_services", "buckets", "sizes"}
_ESCAPES = {"\\": "\\\\", '"': '\\"', "\b": "\\b", "\t": "\\t", "\n": "\\n",
            "\f": "\\f", "\r": "\\r"}

def _scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        # A basic string holds no raw control character: a PEM's newlines
        # (as in [oidc].ca_cert) become \n. And boxd refuses a config file
        # with "{{" anywhere in it, an unrendered template placeholder
        # (BoxdConfig::load), so a value's second brace is escaped.
        escaped = "".join(
            _ESCAPES.get(c)
            or (f"\\u{ord(c):04X}" if ord(c) < 0x20 or c == "\x7f" or (c == "{" and prev == "{")
                else c)
            for prev, c in zip(" " + v, v))
        return f'"{escaped}"'
    raise TypeError(f"cannot emit {type(v)}: {v!r}")

def _inline_table(d: dict) -> str:
    return "{ " + ", ".join(f"{k} = {_scalar(v)}" for k, v in d.items()) + " }"

def _array(key: str, values: list) -> str:
    if values and all(isinstance(v, dict) for v in values):
        items = ",\n".join(f"    {_inline_table(v)}" for v in values)
        return f"{key} = [\n{items},\n]"
    if len(values) > 4:
        items = ",\n".join(f"    {_scalar(v)}" for v in values)
        return f"{key} = [\n{items},\n]"
    return f"{key} = [" + ", ".join(_scalar(v) for v in values) + "]"

def _emit_table(path: str, table: dict, out: list[str]) -> None:
    scalars, subtables, aots = [], [], []
    for k, v in table.items():
        if isinstance(v, dict):
            subtables.append((k, v))
        elif isinstance(v, list) and k in AOT_KEYS:
            aots.append((k, v))
        else:
            scalars.append((k, v))

    if path:
        out.append(f"[{path}]")
    for k, v in scalars:
        out.append(_array(k, v) if isinstance(v, list) else f"{k} = {_scalar(v)}")
    if path or scalars:
        out.append("")
    for k, v in subtables:
        _emit_table(f"{path}.{k}" if path else k, v, out)
    for k, entries in aots:
        for entry in entries:
            out.append(f"[[{path}.{k}]]" if path else f"[[{k}]]")
            for ek, ev in entry.items():
                out.append(_array(ek, ev) if isinstance(ev, list) else f"{ek} = {_scalar(ev)}")
            out.append("")

def dumps(doc: dict, header: str = "") -> str:
    out: list[str] = []
    if header:
        out.extend(f"# {line}".rstrip() for line in header.splitlines())
        out.append("")
    _emit_table("", doc, out)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"
