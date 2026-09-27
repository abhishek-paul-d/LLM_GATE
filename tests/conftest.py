import sys
from pathlib import Path

# Import from src/ without an installed package. Replace with an editable install once pyproject.toml exists.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
