"""Make `anomaly` importable when pytest runs outside the container (inside, PYTHONPATH=/srv/src)."""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))