"""Индекс файлов тома в SQLite: заполняется через `fls`, читается интерфейсом лениво."""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass

INDEX_DIR = os.path.expanduser("~/.local/share/disk-plus/index")
BATCH = 2000


@dataclass
class Node:
    id: int
    parent: int
    name: str
    path: str
    inode: str
    is_dir: bool
    deleted: bool
    realloc: bool
    size: int
    mtime: int
    sys: bool
    meta: str = ""


COLS = "id,parent,name,path,inode,is_dir,deleted,realloc,size,mtime,sys,meta"


def _node(r) -> Node:
    return Node(r[0], r[1], r[2], r[3], r[4], bool(r[5]), bool(r[6]), bool(r[7]), r[8], r[9], bool(r[10]), r[11] or "")


def db_path_for(source: str, offset: int, size: int, deep: bool = False) -> str:
    try:
        mt = int(os.stat(source).st_mtime) if os.path.isfile(source) else 0
    except OSError:
        mt = 0
    key = hashlib.sha1(f"{source}|{offset}|{size}|{mt}|{'deep' if deep else ''}".encode()).hexdigest()[:16]
    os.makedirs(INDEX_DIR, exist_ok=True)
    return os.path.join(INDEX_DIR, key + ".sqlite")


class Index:
    def __init__(self, path: str):
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS files(
                id INTEGER PRIMARY KEY, parent INTEGER, name TEXT, path TEXT, inode TEXT,
                is_dir INTEGER, deleted INTEGER, realloc INTEGER, size INTEGER, mtime INTEGER, sys INTEGER, meta TEXT);
            CREATE INDEX IF NOT EXISTS i_parent ON files(parent);
            CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        """)
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(files)")]
        if "meta" not in cols:      # индекс старой версии
            self.db.execute("ALTER TABLE files ADD COLUMN meta TEXT")

    # --- метаданные -------------------------------------------------------------
    def set_meta(self, k, v):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))
            self.db.commit()

    def meta(self, k, default=None):
        with self.lock:
            r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    @property
    def complete(self) -> bool:
        return self.meta("complete") == "1"

    def counts(self):
        with self.lock:
            t = self.db.execute("SELECT COUNT(*) FROM files WHERE is_dir=0 AND sys=0").fetchone()[0]
            d = self.db.execute("SELECT COUNT(*) FROM files WHERE is_dir=0 AND sys=0 AND deleted=1").fetchone()[0]
        return t, d

    def clear(self):
        with self.lock:
            self.db.execute("DELETE FROM files")
            self.db.execute("DELETE FROM meta")
            self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()

    # --- чтение -----------------------------------------------------------------
    def children(self, parent: int, show_system=False, deleted_only=False) -> list[Node]:
        q = f"SELECT {COLS} FROM files WHERE parent=?"
        if not show_system:
            q += " AND sys=0"
        if deleted_only:
            q += " AND (deleted=1 OR is_dir=1)"
        q += " ORDER BY is_dir DESC, name COLLATE NOCASE"
        with self.lock:
            return [_node(r) for r in self.db.execute(q, (parent,))]

    def get(self, node_id: int) -> Node | None:
        with self.lock:
            r = self.db.execute(f"SELECT {COLS} FROM files WHERE id=?", (node_id,)).fetchone()
        return _node(r) if r else None

    def search(self, text: str, deleted_only=False, limit=3000) -> list[Node]:
        q = f"SELECT {COLS} FROM files WHERE is_dir=0 AND sys=0"
        args: list = []
        if text:
            q += " AND name LIKE ? ESCAPE '\\'"
            args.append("%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if deleted_only:
            q += " AND deleted=1"
        q += " ORDER BY path COLLATE NOCASE LIMIT ?"
        args.append(limit)
        with self.lock:
            return [_node(r) for r in self.db.execute(q, args)]

    def descendants(self, dir_path: str) -> list[Node]:
        """Все узлы внутри каталога (рекурсивно), по пути."""
        like = dir_path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"
        with self.lock:
            return [_node(r) for r in self.db.execute(
                f"SELECT {COLS} FROM files WHERE path LIKE ? ESCAPE '\\' ORDER BY path", (like,))]

    def show_orphans_if_any(self):
        """Папка $OrphanFiles видна, только если в ней есть что показать."""
        with self.lock:
            self.db.execute("UPDATE files SET sys=0 WHERE path='$OrphanFiles' AND EXISTS("
                            "SELECT 1 FROM files c WHERE c.parent=files.id AND c.sys=0)")
            self.db.commit()

    # --- запись -----------------------------------------------------------------
    def insert_many(self, rows):
        with self.lock:
            self.db.executemany(f"INSERT INTO files({COLS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", [tuple(r) + ("",) * (12 - len(r)) for r in rows])
            self.db.commit()


def parse_body_line(line: str):
    """Строка формата body (`fls -m`) -> (path, inode, mode, size, mtime, deleted, realloc) или None."""
    f = line.rstrip("\n").split("|")
    if len(f) < 11:
        return None
    inode, mode, _uid, _gid, size, _a, mtime, _c, _cr = f[-9:]
    name = "|".join(f[1:-9])
    deleted = realloc = False
    if name.endswith(" (deleted)"):
        name, deleted = name[:-10], True
    elif name.endswith(" (deleted-realloc)"):
        name, deleted, realloc = name[:-18], True, True
    if name.endswith(" ($FILE_NAME)"):
        return None
    try:
        return name, inode, mode, int(size), int(mtime), deleted, realloc
    except ValueError:
        return None


def scan_volume(source: str, offset: int, sector: int, index: Index, on_progress=None,
                cancel: threading.Event | None = None) -> tuple[bool, str]:
    """Заполнить индекс через `fls -r -m /`. Возвращает (успех, текст ошибки)."""
    index.clear()
    cmd = ["fls", "-r", "-m", "/", "-b", str(sector), "-o", str(offset), source]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
    except OSError as e:
        return False, f"не удалось запустить fls: {e.strerror}"
    dirs = {"": 0}
    next_id = 1
    batch = []
    n = 0
    t0 = time.time()
    last = 0.0
    for line in p.stdout:
        if cancel is not None and cancel.is_set():
            p.kill()
            p.wait()
            return False, "отменено"
        r = parse_body_line(line)
        if not r:
            continue
        name_path, inode, mode, size, mtime, deleted, realloc = r
        rel = name_path.lstrip("/")
        parent_path, _, base = rel.rpartition("/")
        if not rel:
            continue
        if ":" in base and re.search(r"-(128|144)-\d+$", inode):   # именованные потоки NTFS (файл:поток, $BadClus:$Bad)
            continue
        parent = dirs.get(parent_path, 0)
        is_dir = mode.split("/", 1)[0] in ("d", "V", "v")
        top = rel.split("/", 1)[0]
        sys = int(top.startswith("$"))   # служебные файлы ФС скрываем по умолчанию
        if top == "$OrphanFiles" and rel != top and size > 0:   # «осиротевшие» записи без данных — шум, с данными — ценность
            sys = 0
        nid = next_id
        next_id += 1
        if is_dir:
            dirs[rel] = nid
        batch.append((nid, parent, base, rel, inode, int(is_dir), int(deleted), int(realloc), size, mtime, sys, ""))
        n += 1
        if len(batch) >= BATCH:
            index.insert_many(batch)
            batch = []
        now = time.time()
        if on_progress and now - last > 0.4:
            last = now
            on_progress(n, now - t0)
    if batch:
        index.insert_many(batch)
    err = p.stderr.read()
    rc = p.wait()
    if n == 0 and rc != 0:
        return False, (err.strip().splitlines() or ["fls завершился с ошибкой"])[-1]
    index.show_orphans_if_any()
    index.set_meta("complete", 1)
    index.set_meta("scanned_at", int(time.time()))
    if on_progress:
        on_progress(n, time.time() - t0)
    return True, ""
