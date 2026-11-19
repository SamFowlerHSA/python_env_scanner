"""Convenience launcher: run the scanner without installing the package."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from env_scanner.inventory import main  # noqa: E402

if __name__ == "__main__":
    main()
