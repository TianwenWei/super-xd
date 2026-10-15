import sys
from pathlib import Path

# Engineering evals reuse the explicit model/vehicle doubles defined under tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tests'))
