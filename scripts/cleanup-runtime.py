#!/usr/bin/env python3
"""Preview or explicitly delete Agent Forge execution records."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from forge.runtime_cleanup import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
