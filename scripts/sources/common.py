"""Shared helpers for every data source: paths, config, logging, HTTP with retry."""
from __future__ import annotations

import json
import logging
import random
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
RESOURCES = ROOT / "resources"
DATA = ROOT / "data"
MATCHES = DATA / "matches"
OUTPUT = ROOT / "output"
LOGS = ROOT / "logs"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def load_config() -> dict:
    return json.loads((RESOURCES / "config.json").read_text(encoding="utf-8-sig"))


def setup_logging(name: str) -> logging.Logger:
    LOGS.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")

    fh = logging.FileHandler(LOGS / "update.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # stdout, not the default stderr: PowerShell 5.1 turns a native command's stderr
    # writes into error records, so ordinary log lines would read as step failures.
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        # utf-8-sig, not utf-8: PowerShell's Set-Content -Encoding utf8 writes a BOM,
        # and a leading BOM makes json.loads fail. This strips it when present and
        # behaves identically when it is not.
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, payload) -> None:
    """Atomic write, so an interrupted run never leaves a half-written file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class SourceError(RuntimeError):
    """Raised when a source cannot satisfy a request; makes the ladder fall through."""


def http_get(url, *, headers, params=None, retries=5, backoff=3.0, timeout=45, logger=None):
    """GET with exponential backoff and jitter.

    365scores throttles rapid sequential game requests - a read timeout followed by a
    run of 504s was reproduced during development. Backoff grows quickly and is
    jittered so a burst of retries does not re-synchronise into another burst.
    """
    last = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise SourceError(f"HTTP {resp.status_code} from {url}")
            resp.raise_for_status()
            return resp
        except Exception as exc:  # noqa: BLE001 - any transport failure is retryable
            last = exc
            if logger:
                logger.warning("attempt %d/%d failed for %s: %s", attempt, retries, url, exc)
            if attempt < retries:
                time.sleep(backoff * (2 ** (attempt - 1)) + random.uniform(0, 1.5))
    raise SourceError(f"all {retries} attempts failed for {url}: {last}")
