import sys
sys.path.insert(0, "src")
from chaosx_bot.config import load_settings
from pathlib import Path
s = load_settings()
for name in ("chaos_redux_repo", "focus_tree_repo"):
    p = getattr(s, name, None)
    print(name, "=", p, "exists:", Path(str(p)).exists() if p else None)
