"""
Read-only source-code browser for the "Source Code" tab in the dashboard —
lets you browse and view every file inside scanner/ (this app's own folder)
from the web UI, without needing an editor or terminal open. View-only: no
route in this module ever writes to disk. Deliberately scoped to THIS
folder only (scanner/), not the rest of the trading-platform repo.
"""
import os
from pathlib import Path

SCANNER_DIR = Path(__file__).resolve().parent

# Folders never shown in the tree: caches/data (large, not "code", and
# cache/ holds the Kite access_token.json — never expose that), version
# control internals, Python bytecode caches, and IDE/OS junk.
EXCLUDED_DIR_NAMES = {
    "__pycache__", ".git", ".idea", ".vscode", "node_modules",
    "cache", "data", "results", "static",
}

# Extensions treated as viewable "code/text" — anything else (images,
# binaries, .parquet, etc.) is listed in the tree (so the folder structure
# is still visible) but can't be opened.
VIEWABLE_EXTENSIONS = {
    ".py", ".html", ".htm", ".js", ".css", ".json", ".md", ".txt",
    ".cfg", ".ini", ".toml", ".yml", ".yaml", ".sh", ".bat", ".gitignore",
}

# Filenames never viewable or downloadable even if the extension matches —
# secrets/credentials, regardless of what folder they're found in.
SENSITIVE_NAME_FRAGMENTS = ("env", "token", "secret", "credential", "key", "password")

MAX_VIEW_BYTES = 2 * 1024 * 1024  # 2MB safety cap so a huge file can't hang the UI


def _is_sensitive(name: str) -> bool:
    lower = name.lower()
    return any(frag in lower for frag in SENSITIVE_NAME_FRAGMENTS)


def _is_excluded_dir(name: str) -> bool:
    return name in EXCLUDED_DIR_NAMES or name.startswith(".")


def _safe_resolve(rel_path: str) -> "Path | None":
    """Resolve a user-supplied relative path against SCANNER_DIR, refusing
    anything that escapes it (path traversal) or that isn't a real file."""
    if not rel_path:
        return None
    try:
        candidate = (SCANNER_DIR / rel_path).resolve()
    except (OSError, ValueError):
        return None
    try:
        candidate.relative_to(SCANNER_DIR)
    except ValueError:
        return None  # escaped SCANNER_DIR (e.g. "../../etc/passwd")
    if not candidate.is_file():
        return None
    if _is_sensitive(candidate.name):
        return None
    for part in candidate.relative_to(SCANNER_DIR).parts[:-1]:
        if _is_excluded_dir(part):
            return None
    return candidate


def build_tree() -> dict:
    """Nested {name, type, path, children?, viewable?, size?} tree, dirs
    first then files, both alphabetical."""
    def walk(dir_path: Path) -> list:
        entries = []
        try:
            children = sorted(dir_path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return entries
        for child in children:
            if child.is_dir():
                if _is_excluded_dir(child.name):
                    continue
                sub = walk(child)
                if sub:  # skip empty dirs (nothing viewable inside)
                    entries.append({
                        "name": child.name,
                        "type": "dir",
                        "path": str(child.relative_to(SCANNER_DIR)).replace("\\", "/"),
                        "children": sub,
                    })
            else:
                if _is_sensitive(child.name):
                    continue
                ext = child.suffix.lower()
                try:
                    size = child.stat().st_size
                except OSError:
                    size = None
                entries.append({
                    "name": child.name,
                    "type": "file",
                    "path": str(child.relative_to(SCANNER_DIR)).replace("\\", "/"),
                    "viewable": ext in VIEWABLE_EXTENSIONS or child.name == ".gitignore",
                    "size": size,
                })
        return entries

    return {"name": "scanner", "type": "dir", "path": "", "children": walk(SCANNER_DIR)}


def read_file(rel_path: str) -> "dict | None":
    path = _safe_resolve(rel_path)
    if path is None:
        return None
    ext = path.suffix.lower()
    if ext not in VIEWABLE_EXTENSIONS and path.name != ".gitignore":
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > MAX_VIEW_BYTES:
        return {
            "path": rel_path, "size": size, "truncated": True,
            "content": f"[file too large to display in-browser: {size:,} bytes — open it in your editor]",
            "lines": 0,
        }
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"path": rel_path, "size": size, "truncated": False,
                "content": f"[error reading file: {e}]", "lines": 0}
    return {
        "path": rel_path, "size": size, "truncated": False,
        "content": content, "lines": content.count("\n") + 1,
    }


def search(query: str, max_results: int = 200) -> list:
    """Case-insensitive substring search across filenames AND file contents
    of every viewable file. Returns [{path, filename_match, line_matches:
    [{line_no, text}]}], capped at max_results files."""
    query = (query or "").strip()
    if not query:
        return []
    q = query.lower()
    results = []

    def walk(dir_path: Path):
        try:
            children = sorted(dir_path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return
        for child in children:
            if len(results) >= max_results:
                return
            if child.is_dir():
                if _is_excluded_dir(child.name):
                    continue
                walk(child)
                continue
            if _is_sensitive(child.name):
                continue
            ext = child.suffix.lower()
            if ext not in VIEWABLE_EXTENSIONS and child.name != ".gitignore":
                continue
            rel = str(child.relative_to(SCANNER_DIR)).replace("\\", "/")
            filename_match = q in child.name.lower()
            line_matches = []
            try:
                with open(child, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, start=1):
                        if q in line.lower():
                            line_matches.append({"line_no": i, "text": line.rstrip("\n")[:300]})
                            if len(line_matches) >= 8:
                                break
            except OSError:
                pass
            if filename_match or line_matches:
                results.append({"path": rel, "filename_match": filename_match, "line_matches": line_matches})

    walk(SCANNER_DIR)
    return results
