import sys
from pathlib import Path

# Make `import shopbot` and `from harness import ...` work when running pytest.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
