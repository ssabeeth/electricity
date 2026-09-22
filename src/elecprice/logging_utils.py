from __future__ import annotations

import logging
import os

_CONFIGURED = False


def get_logger(name: str) -> logging.Logger:
    global _CONFIGURED
    if not _CONFIGURED:
        logging.basicConfig(
            level=os.environ.get("ELEC_LOG_LEVEL", "INFO"),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        for noisy in ("matplotlib", "PIL", "urllib3", "alembic", "httpx"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        _CONFIGURED = True
    return logging.getLogger(name)
