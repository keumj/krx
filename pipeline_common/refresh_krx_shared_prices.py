from __future__ import annotations

from pipeline_krx.refresh_prices import main, refresh_krx_prices


__all__ = ["main", "refresh_krx_prices"]


if __name__ == "__main__":
    raise SystemExit(main())
