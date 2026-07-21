"""Entrypoint for the discovery job (§6). See .github/workflows/discovery.yml."""
import logging
from pathlib import Path

from fetchers.discovery import run_discovery

ROOT = Path(__file__).resolve().parent.parent

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_discovery(ROOT / "config" / "companies_seed.yaml", ROOT / "config" / "ats_map.yaml")
