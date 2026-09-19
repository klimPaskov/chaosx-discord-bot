from pathlib import Path
p = Path("/srv/chaosx/chaosx-discord-bot/src/chaosx_bot/indexer.py")
s = p.read_text(encoding="utf-8")
old = '''def qoder_source_class_for(path: str) -> str:
    if path.startswith("repo-knowledge/"):
        return "qoder_repo_knowledge"
    if path.startswith("codebase/"):
        return "qoder_codebase"
    return "qoder_doc"'''
new = '''def qoder_source_class_for(path: str) -> str:
    """Classify a Qoder document from its path inside the configured root.

    Segment-based, not prefix-based: the root may be a parent directory holding
    both the in-repo repowiki (``repowiki/en/content/...``) and the older Qoder
    IDE store (``chaos_redux/master__en-US/repo-knowledge/...``). Prefix checks
    silently downgraded every document to ``qoder_doc`` once the root moved up
    one level (2026-09-19).
    """
    segments = [part.lower() for part in path.replace("\\\\", "/").split("/") if part]
    if "repo-knowledge" in segments or "repowiki" in segments:
        return "qoder_repo_knowledge"
    if "codebase" in segments:
        return "qoder_codebase"
    return "qoder_doc"'''
assert old in s, "anchor not found"
p.write_text(s.replace(old, new), encoding="utf-8")
print("patched qoder_source_class_for")
