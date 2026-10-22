"""Reproduce the repository CI YAML/tag checks locally without changing files."""
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
ALLOWED = {"Data", "Infrastructure", "Business", "Cloud", "Core", "AI", "Getting Started", "System"}
files = sorted((ROOT / "flows").rglob("*.yaml"))
failures = []
for path in files:
    try:
        flow = yaml.safe_load(path.read_text(encoding="utf-8"))
        tags = flow["extend"]["tags"]
        if not isinstance(tags, list) or len(tags) > 3 or not set(tags) <= ALLOWED:
            raise ValueError("Invalid tags or more than three tags")
    except Exception as error:
        failures.append(f"{path.relative_to(ROOT)}: {type(error).__name__}: {error}")
print(f"Checked {len(files)} flow YAML files; {len(failures)} failures.")
for failure in failures:
    print(failure)
sys.exit(bool(failures))
