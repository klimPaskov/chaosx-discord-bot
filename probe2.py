import json, sys
sys.path.insert(0, "src")
from chaosx_bot.config import load_settings
from chaosx_bot.knowledge import Knowledge
qs = ["what is the chaos meter", "how do super events work", "what is the holy realm",
      "tell me about event 2", "how do I install Chaos Redux", "where is the AI defined"]
s = load_settings()
k = Knowledge(s.chaos_redux_repo, s.db_path, s.obsidian_vault_path, s.qoder_repowiki_path,
              catalog_repo=s.focus_tree_repo or s.chaos_redux_repo, autorebuild=False)
print(json.dumps({q: [r[0] for r in k._public_ask_ranked_rows(q, 6)] for q in qs}, indent=1))
