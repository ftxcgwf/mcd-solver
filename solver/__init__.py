# -*- coding: utf-8 -*-
"""今天麦什么 —— 麦当劳点单决策求解器。"""

from .model import Item, load_items, filter_usable, role_label, dedup
from .satisfaction import (
    PERSONAS, SODIUM_DAILY_LIMIT, score, objective, resolve_persona, totals,
)
from .optimize import Constraints, solve
from .taste import TEXTURES, TEXTURE_ZH

__version__ = "0.1.0"
__all__ = [
    "Item", "load_items", "filter_usable", "role_label", "dedup",
    "PERSONAS", "SODIUM_DAILY_LIMIT", "score", "objective", "resolve_persona",
    "totals",
    "Constraints", "solve", "TEXTURES", "TEXTURE_ZH",
]
