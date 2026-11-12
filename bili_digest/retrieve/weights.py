# -*- coding: utf-8 -*-
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

TOML_PATH = Path(__file__).parent / "weights.toml"

REQUIRED_KINDS = ("subtitle", "subtitle_seg", "summary", "key_points", "insights",
                  "audience", "growth", "meta", "annotation", "comment", "danmaku")

class ConfigError(ValueError):
    """配置缺键或越界 —— 一律抛出来，不静默兜底"""

def _check_range(name, value, lo, hi):
    if not (lo <= value <= hi):
        raise ConfigError(f"{name}={value} 越界，应在 [{lo}, {hi}]")

def load(path=None):
    p = Path(path or TOML_PATH)
    with p.open("rb") as f:
        raw = tomllib.load(f)
    sc = raw.get("scoring") or {}
    missing = [k for k in ("literal_boost", "short_chars", "short_penalty",
                           "refute_penalty") if k not in sc]
    if missing:
        raise ConfigError(f"{p.name} 缺少 scoring.{missing}")
    kw = dict(sc.get("kind_weight") or {})
    missing = [k for k in REQUIRED_KINDS if k not in kw]
    if missing:
        raise ConfigError(f"{p.name} 的 kind_weight 缺少 {missing}")

    cfg = {
        "literal_boost": float(sc["literal_boost"]),
        "short_chars": int(sc["short_chars"]),
        "short_penalty": float(sc["short_penalty"]),

        "refute_penalty": float(sc["refute_penalty"]),
        "kind_weight": {k: float(v) for k, v in kw.items()},

        "long_query_chars": int(sc.get("long_query_chars", 15)),
        "long_chat_penalty": float(sc.get("long_chat_penalty", 0.82)),
    }
    _check_range("literal_boost", cfg["literal_boost"], 1.0, 10.0)
    _check_range("short_penalty", cfg["short_penalty"], 0.0, 1.0)
    _check_range("short_chars", cfg["short_chars"], 1, 400)
    _check_range("long_chat_penalty", cfg["long_chat_penalty"], 0.0, 1.0)
    _check_range("long_query_chars", cfg["long_query_chars"], 1, 500)
    _check_range("refute_penalty", cfg["refute_penalty"], 0.0, 1.0)
    for k, v in cfg["kind_weight"].items():
        _check_range(f"kind_weight.{k}", v, 0.0, 2.0)
    return cfg

def format_toml(cfg, template=None):
    text = template if template is not None else TOML_PATH.read_text(encoding="utf-8")
    out, in_kind = [], False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[scoring.kind_weight]"):
            in_kind = True
            out.append(line)
            continue
        if s.startswith("[") and not s.startswith("[scoring.kind_weight]"):
            in_kind = False
        if "=" in s and not s.startswith("#"):
            key = s.split("=", 1)[0].strip()
            new = None
            if in_kind and key in cfg["kind_weight"]:
                new = _fmt(cfg["kind_weight"][key])
            elif not in_kind and key in ("literal_boost", "short_chars", "short_penalty"):
                new = _fmt(cfg[key])
            if new is not None:
                indent = line[:len(line) - len(line.lstrip())]
                out.append(f"{indent}{key} = {new}")
                continue
        out.append(line)
    return "\n".join(out) + "\n"

def _fmt(v):
    return str(int(v)) if isinstance(v, int) else repr(float(v))

def reload(cfg=None):
    from bili_digest.retrieve import rag_core as rc
    c = cfg or load()
    rc.LITERAL_BOOST = c["literal_boost"]
    rc.SHORT_CHARS = c["short_chars"]
    rc.SHORT_PENALTY = c["short_penalty"]
    rc.KIND_WEIGHT = dict(c["kind_weight"])
    return c
