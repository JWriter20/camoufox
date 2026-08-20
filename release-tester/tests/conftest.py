import sys
from pathlib import Path

# `src` is imported as a package relative to release-tester/, matching how
# run.py puts its own directory on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
