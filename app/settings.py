from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    app_name: str = "Keumjm Portfolio Lab"
    host: str = os.getenv("KEUMJM_HOST", "0.0.0.0")
    port: int = int(os.getenv("KEUMJM_PORT", "8515"))
    project_root: Path = Path(__file__).resolve().parents[1]


settings = Settings()

