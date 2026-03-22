from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


@lru_cache
def get_scoring_config() -> dict:
    return json.loads((CONFIG_DIR / "scoring.json").read_text(encoding="utf-8"))


@lru_cache
def get_peer_group_config() -> dict:
    return json.loads((CONFIG_DIR / "peer_groups.json").read_text(encoding="utf-8"))

