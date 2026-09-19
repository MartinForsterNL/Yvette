"""Talk server memory - a small second brain, per profile.

Each profile gets its own brain directory (brain/<profile>/) and its own
SQLite index (brain/<profile>/index.db). This keeps memory fully separated
between profiles, so clearing one profile's memory leaves the others intact.

Pattern mirrors agent-yvette's BrainIndex:
  - .md files hold daily notes, each with "## slug" blocks.
  - SQLite is a derived index (id, date, slug, body, embedding, mtime) + FTS5.
  - Hybrid search: vector (cosine) + FTS5 keyword, with a recency boost.
  - search_block(query, profile) returns an injection block for the system prompt.

The .md files are the source of truth. The index is rebuildable. Never raises
from search - degrades to keyword-only if the embedder is unavailable.
"""

import os
import re
import sqlite3
import datetime

import numpy as np

_STOP_WORDS = {
    "a", "an", "the", "and", "or", "but", "if", "of", "at", "by", "for",
    "in", "on", "to", "is", "are", "was", "were", "be", "been", "being",
    "am", "do", "does", "did", "have", "has", "had", "will", "would",
    "can", "could", "shall", "should", "may", "might", "must", "not",
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her",
    "us", "them", "my", "your", "his", "its", "our", "their", "this",
    "that", "these", "those", "what", "which", "who", "whom", "when",
    "where", "why", "how", "all", "any", "some", "as", "so", "than",
    "too", "very", "just", "about", "into", "over", "after", "before",
    "also", "there", "here", "each", "few", "more", "most", "other",
    "from", "with", "out", "up", "down", "now", "then", "again",
}


class Memory:
    def __init__(self, root: str, config: dict, embedder=None):
        self.root = root
        cfg = config.get("memory", {}) or {}
        self.brain_dir = os.path.join(root, cfg.get("brain_dir", "brain"))
        self.embedder = embedder
        self.top_k = int(cfg.get("top_k", 5))
        self.min_score = float(cfg.get("min_score", 0.3))
        self.max_chars = int(cfg.get("max_chars", 1200))
        self.body_chars = int(cfg.get("body_chars", 200))
        self.recency_decay = float(cfg.get("recency_decay", 0.1))
        self.recency_floor = float(cfg.get("recency_floor", 0.5))
        os.makedirs(self.brain_dir, exist_ok=True)

    # -- profile paths --------------------------------------------------
    def _profile_key(self, profile) -> str:
        p = (profile or "").strip() or "default"
        return p.replace("/", "_").replace("\\", "_").replace("..", "_")

    def _profile_dir(self, profile) -> str:
        return os.path.join(self.brain_dir, self._profile_key(profile))

    def _profile_db_path(self, profile) -> str:
        return os.path.join(self._profile_dir(profile), "index.db")

    # -- db -------------------------------------------------------------
    def _db(self, profile="default"):
        os.makedirs(self._profile_dir(profile), exist_ok=True)
        conn = sqlite3.connect(self._profile_db_path(profile))
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY,
                date TEXT,
                slug TEXT,
                body TEXT,
                embedding BLOB,
                file_mtime REAL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(slug, body);
        """)
        conn.commit()
        return conn

    # -- parse ----------------------------------------------------------
    def _parse_file(self, fname: str, content: str, mtime: float) -> list:
        items = []
        date = fname[:-3] if fname.endswith(".md") else fname
        blocks = re.split(r"^## ", content, flags=re.MULTILINE)[1:]
        for block in blocks:
            lines = block.split("\n")
            slug = lines[0].strip()
            if not slug:
                continue
            body = "\n".join(lines[1:]).strip()
            if not body:
                continue
            items.append({
                "id": f"{date}#{slug}",
                "date": date,
                "slug": slug,
                "body": body,
                "file_mtime": mtime,
            })
        return items

    # -- embed ----------------------------------------------------------
    def _embed(self, text):
        if not self.embedder or not self.embedder.available():
            return None
        try:
            return self.embedder.embed([text])[0]
        except Exception:
            return None

    # -- reindex --------------------------------------------------------
    def reindex(self, profile="default"):
        try:
            conn = self._db(profile)
        except Exception as e:
            print(f"[memory] db init failed: {e}")
            return
        try:
            cur = conn.execute("SELECT id, file_mtime, embedding FROM items")
            old = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

            items = []
            pdir = self._profile_dir(profile)
            if os.path.isdir(pdir):
                for fname in sorted(os.listdir(pdir)):
                    if not fname.endswith(".md"):
                        continue
                    fpath = os.path.join(pdir, fname)
                    try:
                        mtime = os.path.getmtime(fpath)
                        with open(fpath, encoding="utf-8") as f:
                            content = f.read()
                    except OSError:
                        continue
                    items.extend(self._parse_file(fname, content, mtime))

            new_ids = {i["id"] for i in items}
            for old_id in old:
                if old_id not in new_ids:
                    conn.execute("DELETE FROM items WHERE id = ?", (old_id,))
                    conn.execute("DELETE FROM items_fts WHERE rowid = (SELECT rowid FROM items WHERE id = ?)", (old_id,))

            for item in items:
                old_mtime, old_emb = old.get(item["id"], (None, None))
                if old_mtime == item["file_mtime"] and old_emb is not None:
                    emb = old_emb
                else:
                    vec = self._embed(item["slug"] + ". " + item["body"])
                    emb = vec.tobytes() if vec is not None else None
                conn.execute("""
                    INSERT OR REPLACE INTO items (id, date, slug, body, embedding, file_mtime)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (item["id"], item["date"], item["slug"], item["body"], emb, item["file_mtime"]))
                rowid = conn.execute("SELECT rowid FROM items WHERE id = ?", (item["id"],)).fetchone()[0]
                conn.execute("DELETE FROM items_fts WHERE rowid = ?", (rowid,))
                conn.execute("INSERT INTO items_fts (rowid, slug, body) VALUES (?, ?, ?)",
                             (rowid, item["slug"], item["body"]))
            conn.commit()
        except Exception as e:
            print(f"[memory] reindex failed: {e}")
        finally:
            conn.close()

    # -- search ---------------------------------------------------------
    def _fts_query(self, query):
        tokens = re.findall(r"\w+", query or "")
        content = [t for t in tokens if t.lower() not in _STOP_WORDS]
        if not content:
            return ""
        return " OR ".join(f'"{t}"' for t in content)

    def _search(self, query, profile="default"):
        try:
            conn = self._db(profile)
        except Exception:
            return []
        try:
            cur = conn.execute("SELECT id, slug, body, embedding, date FROM items")
            rows = cur.fetchall()
            if not rows:
                return []

            fts_ids = set()
            fts_q = self._fts_query(query)
            if fts_q:
                try:
                    cur = conn.execute("SELECT rowid FROM items_fts WHERE items_fts MATCH ?", (fts_q,))
                    for (rowid,) in cur.fetchall():
                        r = conn.execute("SELECT id FROM items WHERE rowid = ?", (rowid,)).fetchone()
                        if r:
                            fts_ids.add(r[0])
                except Exception:
                    pass

            qv = None
            if self.embedder and self.embedder.available():
                try:
                    qv = self.embedder.embed([query])[0]
                except Exception:
                    qv = None

            scores = {}
            for item_id, slug, body, emb_blob, date in rows:
                score = 0.0
                if qv is not None and emb_blob:
                    try:
                        emb = np.frombuffer(emb_blob, dtype=np.float32)
                        score = float(np.dot(emb, qv))
                    except Exception:
                        score = 0.0
                if item_id in fts_ids:
                    score += 0.15
                scores[item_id] = score

            today = datetime.date.today()
            for item_id in scores:
                try:
                    d = datetime.date.fromisoformat(item_id.split("#")[0])
                    days = (today - d).days
                    scores[item_id] *= max(self.recency_floor, 1.0 - self.recency_decay * days)
                except Exception:
                    pass

            order = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
            out = []
            for item_id in order[:self.top_k]:
                if scores[item_id] < self.min_score:
                    break
                out.append((item_id, scores[item_id]))
            return out
        except Exception:
            return []
        finally:
            conn.close()

    def search_block(self, query, profile="default"):
        try:
            results = self._search(query, profile)
            if not results:
                return ""
            conn = self._db(profile)
            try:
                lines = []
                for item_id, _score in results:
                    row = conn.execute("SELECT slug, body, date FROM items WHERE id = ?", (item_id,)).fetchone()
                    if row:
                        slug, body, date = row
                        snippet = body[:self.body_chars]
                        lines.append(f"### {slug} ({date})\n{snippet}")
            finally:
                conn.close()
            if not lines:
                return ""
            block = "## Relevant memory\n" + "\n".join(lines)
            if len(block) > self.max_chars:
                block = block[:self.max_chars].rstrip() + "\n[...]"
            return block
        except Exception:
            return ""

    # -- write ----------------------------------------------------------
    def add_item(self, text: str, profile="default"):
        """Append a memory entry to the profile's today brain file."""
        if not text or not text.strip():
            return
        date = datetime.date.today().isoformat()
        pdir = self._profile_dir(profile)
        os.makedirs(pdir, exist_ok=True)
        fpath = os.path.join(pdir, f"{date}.md")
        slug = re.sub(r"\s+", "-", text.strip().lower())[:50].strip("-") or "note"
        entry = f"## {slug}\n{text.strip()}\n\n"
        with open(fpath, "a", encoding="utf-8") as f:
            f.write(entry)
        self.reindex(profile)

    def read_day(self, date, profile="default"):
        """Return the full content of a day's memory file ('' or 'today' = today)."""
        d = (date or "").strip().lower()
        today = datetime.date.today()
        if d in ("", "today"):
            d = today.isoformat()
        elif d == "yesterday":
            d = (today - datetime.timedelta(days=1)).isoformat()
        fpath = os.path.join(self._profile_dir(profile), f"{d}.md")
        try:
            with open(fpath, encoding="utf-8") as f:
                content = f.read().strip()
            return content or "no memory for that day"
        except OSError:
            return "no memory for that day"

    def delete_item(self, slug, date=None, profile="default"):
        """Delete one memory item by its slug. Searches the given day (or all days)."""
        slug = (slug or "").strip()
        if not slug:
            return "error: no slug given"
        pdir = self._profile_dir(profile)
        if not os.path.isdir(pdir):
            return "no memory found"
        if date:
            d = (date or "").strip().lower()
            today = datetime.date.today()
            if d in ("", "today"):
                d = today.isoformat()
            elif d == "yesterday":
                d = (today - datetime.timedelta(days=1)).isoformat()
            fnames = [f"{d}.md"]
        else:
            fnames = sorted(f for f in os.listdir(pdir) if f.endswith(".md"))
        for fname in fnames:
            fpath = os.path.join(pdir, fname)
            try:
                with open(fpath, encoding="utf-8") as f:
                    content = f.read()
            except OSError:
                continue
            pattern = re.compile(r"^## " + re.escape(slug) + r"\s*\n.*?(?=^## |\Z)", re.MULTILINE | re.DOTALL)
            new_content = pattern.sub("", content)
            if new_content != content:
                with open(fpath, "w", encoding="utf-8") as f:
                    f.write(new_content.strip() + ("\n" if new_content.strip() else ""))
                self.reindex(profile)
                return f"deleted memory item '{slug}'"
        return f"error: no memory item '{slug}' found"

    def clear(self, profile="default"):
        """Delete one profile's memory: remove its brain .md files and wipe its index."""
        pdir = self._profile_dir(profile)
        if os.path.isdir(pdir):
            for fname in os.listdir(pdir):
                if fname.endswith(".md"):
                    try:
                        os.remove(os.path.join(pdir, fname))
                    except OSError:
                        pass
        try:
            conn = self._db(profile)
            conn.execute("DELETE FROM items")
            conn.execute("DELETE FROM items_fts")
            conn.commit()
            conn.close()
        except Exception as e:  # noqa: BLE001
            print(f"[memory] clear failed: {e}")
