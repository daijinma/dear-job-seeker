"""Provider registry. Import failures of one provider must not break the others
(e.g. a missing optional dependency), so modules load lazily and tolerantly."""
from __future__ import annotations

import importlib

_MODULES = {"nowcoder": "NowCoder", "iguopin": "Iguopin", "chinahr": "Chinahr", "zhaopin": "Zhaopin",
            "mohrss": "Mohrss", "ciic": "Ciic", "bytedance": "Bytedance", "boss": "Boss",
            "yupao": "Yupao", "job51": "Job51", "job58": "Job58", "ganji": "Ganji", "liepin": "Liepin",
            "kq36": "Kq36"}
_cache: dict = {}


def get(name: str):
    """Return a Provider instance by name, or raise KeyError."""
    if name not in _MODULES:
        raise KeyError(f"unknown provider {name!r}; known: {sorted(_MODULES)}")
    if name not in _cache:
        mod = importlib.import_module(f"djs.providers.{name}")
        _cache[name] = getattr(mod, _MODULES[name])()
    return _cache[name]


def names() -> list[str]:
    return list(_MODULES)
