import sys
from pathlib import Path

# Garante que "backend/" está no path ao rodar pytest de qualquer lugar
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
