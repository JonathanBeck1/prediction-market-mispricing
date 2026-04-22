from __future__ import annotations

import asyncio
import logging

from app.runner import run_app


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


if __name__ == "__main__":
    _setup_logging()
    asyncio.run(run_app())

