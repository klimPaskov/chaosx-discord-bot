import json, sys
sys.path.insert(0, "src")
from chaosx_bot.config import load_settings
from chaosx_bot.knowledge import Knowledge

questions = [
    "explain the zombie outbreak event chain",
    "where is the super event text defined",
    "what is the chaos level cap",
    "how do I install the mod",
    "explain the holy realm event chain",
    "where are the AI strategy profiles defined",
    "how do I add a new event",
    "what are the six faction packages",
    "what does the biological warfare system do",
    "how do I report a bug",
    "how do focus trees interact with the chaos meter",
    "how do events evolve if you ignore them",
    "what is the world-end scenario",
    "explain the migration and camps system",
    "where are the event descriptions stored",
]
s = load_settings()
k = Knowledge(s.chaos_redux_repo, s.db_path, s.obsidian_vault_path, s.qoder_repowiki_path,
              catalog_repo=s.focus_tree_repo or s.chaos_redux_repo, autorebuild=False)
out = {}
for q in questions:
    rows = k._public_ask_ranked_rows(q, 8)
    out[q] = [r[0] for r in rows]
print(json.dumps(out, indent=1))
