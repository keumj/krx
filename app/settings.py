from __future__ import annotations

import os
from dataclasses import dataclass
from ipaddress import ip_network
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - startup still works if dotenv is absent.
    load_dotenv = None


if load_dotenv is not None:
    load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    app_name: str = "Keumjm Portfolio Lab"
    host: str = os.getenv("KEUMJM_HOST", "0.0.0.0")
    port: int = int(os.getenv("KEUMJM_PORT", "8515"))
    project_root: Path = Path(__file__).resolve().parents[1]
    access_mode: str = os.getenv("KEUMJM_ACCESS_MODE", "lan").strip().lower()
    allowed_cidrs: tuple[str, ...] = _env_list("KEUMJM_ALLOWED_CIDRS")
    enable_docs: bool = _env_bool("KEUMJM_ENABLE_DOCS", True)

    def parsed_allowed_networks(self):
        return tuple(ip_network(cidr, strict=False) for cidr in self.allowed_cidrs)


settings = Settings()
