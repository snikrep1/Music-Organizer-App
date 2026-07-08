#!/usr/bin/env python3
"""Music Organizer — GUI app for bulk-editing the metadata of music files.

Scan a folder, review every track's tags in an editable table, change them
(individually or in bulk), edit embedded cover art, and write the changes back
into the files in place — with optional backups and one-click undo.

Supports MP3, FLAC, M4A/AAC/ALAC, OGG/Opus and WMA via the `mutagen` library.
Flexoki dark theme.
"""

import os
import sys
import base64
import shutil
import struct
import zlib
import hashlib
import tempfile
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem,
    QTableWidget, QTableWidgetItem, QHeaderView, QSplitter,
    QFileDialog, QDialog, QDialogButtonBox, QMessageBox,
    QProgressBar, QGroupBox, QAbstractItemView,
    QComboBox, QCheckBox, QFrame, QMenu, QInputDialog,
    QTabWidget,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QBrush, QPixmap

# ── Tag library ───────────────────────────────────────────────────────────────

try:
    import mutagen
    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import ID3, ID3NoHeaderError, APIC, COMM
    from mutagen.flac import FLAC, Picture
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.oggvorbis import OggVorbis
    from mutagen.oggopus import OggOpus
    MUTAGEN_AVAILABLE = True

    # EasyID3 has no 'comment' key out of the box — register one that maps to the
    # COMM frame (empty description, English) so MP3 comments round-trip.
    def _comment_get(id3, key):
        return [f.text[0] for f in id3.getall("COMM") if f.desc == "" and f.text]

    def _comment_set(id3, key, value):
        id3.delall("COMM")
        text = value[0] if isinstance(value, list) else value
        id3.add(COMM(encoding=3, lang="eng", desc="", text=text))

    def _comment_delete(id3, key):
        id3.delall("COMM")

    if "comment" not in EasyID3.valid_keys:
        EasyID3.RegisterKey("comment", _comment_get, _comment_set, _comment_delete)
except ImportError:
    MUTAGEN_AVAILABLE = False


AUDIO_EXTENSIONS = {
    '.mp3', '.flac', '.m4a', '.aac', '.alac',
    '.ogg', '.oga', '.opus', '.wma',
}

# Formats whose embedded cover art we can read/write.
ART_EXTENSIONS = {'.mp3', '.flac', '.m4a', '.aac', '.alac', '.ogg', '.oga', '.opus'}

# (tag_key, column_header).  The synthetic "__file__" column shows the filename
# and is read-only; every other column is an editable tag field.
COLUMNS = [
    ("__file__",    "File"),
    ("title",       "Title"),
    ("artist",      "Artist"),
    ("albumartist", "Album Artist"),
    ("album",       "Album"),
    ("date",        "Year"),
    ("genre",       "Genre"),
    ("tracknumber", "Track"),
    ("discnumber",  "Disc"),
    ("comment",     "Comment"),
]
FIELDS = [k for k, _ in COLUMNS if k != "__file__"]
EDITABLE_HEADERS = [(k, h) for k, h in COLUMNS if k != "__file__"]
NUMERIC_FIELDS = {"date", "tracknumber", "discnumber"}

# ── Core tag logic ────────────────────────────────────────────────────────────

def read_tags(path: Path) -> Optional[dict]:
    """Read the FIELDS off a file. Returns None if mutagen can't handle it."""
    if not MUTAGEN_AVAILABLE:
        return None
    try:
        audio = mutagen.File(path, easy=True)
    except Exception:
        return None
    if audio is None:
        return None
    result = {}
    for f in FIELDS:
        try:
            vals = audio.get(f)
        except Exception:
            vals = None
        result[f] = (vals[0] if vals else "") or ""
    return result


def write_tags(path: Path, changes: dict) -> None:
    """Write the given fields back into the file (empty value clears the tag)."""
    audio = mutagen.File(path, easy=True)
    if audio is None:
        raise ValueError("Unsupported audio file")
    if audio.tags is None:
        audio.add_tags()
    for field, value in changes.items():
        if value == "":
            try:
                audio.pop(field, None)
            except Exception:
                pass
        else:
            audio[field] = value
    audio.save()


def validate_value(field: str, value: str) -> bool:
    """True if `value` is acceptable for `field`. Empty is always allowed."""
    value = value.strip()
    if not value:
        return True
    if field == "date":
        head = value.split("-")[0]
        return head.isdigit() and len(head) == 4
    if field in ("tracknumber", "discnumber"):
        parts = value.split("/")
        return all(p.strip().isdigit() for p in parts if p.strip()) and len(parts) <= 2
    return True


def diff_track(track: dict) -> dict:
    """Return {field: new_value} for fields whose edited value differs."""
    return {
        f: track["edited"][f]
        for f in FIELDS
        if track["edited"].get(f, "") != track["original"].get(f, "")
    }


def track_has_edits(track: dict) -> bool:
    return bool(diff_track(track)) or track.get("art_action") is not None


# ── Album art logic ───────────────────────────────────────────────────────────

def detect_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "image/jpeg"


def read_art(path: Path):
    """Return (image_bytes, mime) for the front-cover art, or None if there's none."""
    ext = path.suffix.lower()
    try:
        if ext == ".mp3":
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                return None
            for frame in tags.getall("APIC"):
                return (frame.data, frame.mime or detect_mime(frame.data))
            return None
        if ext == ".flac":
            f = FLAC(path)
            if f.pictures:
                p = f.pictures[0]
                return (p.data, p.mime or detect_mime(p.data))
            return None
        if ext in (".m4a", ".aac", ".alac"):
            a = MP4(path)
            covr = a.tags.get("covr") if a.tags else None
            if covr:
                c = covr[0]
                mime = "image/png" if c.imageformat == MP4Cover.FORMAT_PNG else "image/jpeg"
                return (bytes(c), mime)
            return None
        if ext in (".ogg", ".oga", ".opus"):
            a = OggOpus(path) if ext == ".opus" else OggVorbis(path)
            b64 = a.get("metadata_block_picture")
            if b64:
                pic = Picture(base64.b64decode(b64[0]))
                return (pic.data, pic.mime or detect_mime(pic.data))
            return None
    except Exception:
        return None
    return None


def write_art(path: Path, data: bytes, mime: str) -> None:
    ext = path.suffix.lower()
    if ext == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="", data=data))
        tags.save(path)
    elif ext == ".flac":
        f = FLAC(path)
        f.clear_pictures()
        pic = Picture()
        pic.type = 3
        pic.mime = mime
        pic.data = data
        f.add_picture(pic)
        f.save()
    elif ext in (".m4a", ".aac", ".alac"):
        a = MP4(path)
        if a.tags is None:
            a.add_tags()
        fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
        a["covr"] = [MP4Cover(data, imageformat=fmt)]
        a.save()
    elif ext in (".ogg", ".oga", ".opus"):
        a = OggOpus(path) if ext == ".opus" else OggVorbis(path)
        pic = Picture()
        pic.type = 3
        pic.mime = mime
        pic.data = data
        a["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
        a.save()
    else:
        raise ValueError(f"Album art editing not supported for {ext} files")


def remove_art(path: Path) -> None:
    ext = path.suffix.lower()
    if ext == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            return
        tags.delall("APIC")
        tags.save(path)
    elif ext == ".flac":
        f = FLAC(path)
        f.clear_pictures()
        f.save()
    elif ext in (".m4a", ".aac", ".alac"):
        a = MP4(path)
        if a.tags is not None:
            a.pop("covr", None)
            a.save()
    elif ext in (".ogg", ".oga", ".opus"):
        a = OggOpus(path) if ext == ".opus" else OggVorbis(path)
        a.pop("metadata_block_picture", None)
        a.save()
    else:
        raise ValueError(f"Album art editing not supported for {ext} files")


def apply_art_action(path: Path, action) -> None:
    """action is ('replace', data, mime) or ('remove',)."""
    if action[0] == "remove":
        remove_art(path)
    elif action[0] == "replace":
        write_art(path, action[1], action[2])


# ── File-organize: pattern rendering ──────────────────────────────────────────

# foobar2000-style %tokens% understood by the naming pattern.
PATTERN_TOKENS = [
    "%albumartist%", "%artist%", "%album%", "%title%",
    "%tracknumber%", "%discnumber%", "%year%", "%genre%",
]
# Title-formatting functions supported inside patterns.
PATTERN_FUNCS = ["$num(n,len)", "$if(x,then,else)", "$if2(x,else)",
                 "$upper(x)", "$lower(x)", "$replace(x,from,to)", "$left(x,n)"]

DEFAULT_PATTERN = "%albumartist%/['['%year%'] ']%album%/$num(%tracknumber%,2) - %title%"

_ILLEGAL_CHARS = '<>:"/\\|?*'
_ILLEGAL_TABLE = {ord(c): "_" for c in _ILLEGAL_CHARS}
_ILLEGAL_TABLE.update({i: "_" for i in range(32)})  # control chars

# Defaults used only for a bare (non-bracketed) field that resolves empty, so a
# path segment is never blank. Inside [...] an empty field just hides the section.
_FIELD_DEFAULTS = {
    "albumartist": "Unknown Artist", "artist": "Unknown Artist",
    "album": "Unknown Album", "title": "Untitled",
    "tracknumber": "00", "track": "00", "discnumber": "1", "disc": "1",
    "year": "Unknown Year", "date": "Unknown Year", "genre": "Unknown Genre",
}


def _clean_value(value: str) -> str:
    """Neutralize path separators / illegal chars inside a field value."""
    return (value or "").translate(_ILLEGAL_TABLE)


def _clean_segment(value: str) -> str:
    """Final safety pass on one path segment. '' if the segment is empty."""
    if not value.strip():
        return ""
    v = value.translate(_ILLEGAL_TABLE)
    v = " ".join(v.split()).strip().rstrip(". ")
    return v or "_"


def _first_number(value: str) -> str:
    """'3/12' -> '3', '07' -> '7'; non-numeric returned stripped."""
    return (value or "").split("/")[0].strip()


def _format_field(name: str, tags: dict, stem: str) -> str:
    """Raw value for a field (no defaults). '' means the tag is absent."""
    t = name.lower()
    g = lambda k: (tags.get(k) or "").strip()
    if t == "albumartist":
        return g("albumartist") or g("artist")
    if t == "artist":
        return g("artist") or g("albumartist")
    if t == "album":
        return g("album")
    if t == "title":
        return g("title") or stem
    if t in ("tracknumber", "track"):
        return _first_number(g("tracknumber"))
    if t in ("discnumber", "disc"):
        return _first_number(g("discnumber"))
    if t in ("year", "date"):
        d = g("date")
        return d[:4] if d[:4].isdigit() else d
    if t == "genre":
        return g("genre")
    return ""


# ── Pattern parser (nodes) ────────────────────────────────────────────────────
# Node kinds: ("lit", text) | ("field", name) | ("opt", [nodes])
#             | ("func", name, [ [nodes], ... ])

def _parse_seq(s: str, i: int, stops: str):
    nodes, buf, n = [], [], len(s)

    def flush():
        if buf:
            nodes.append(("lit", "".join(buf)))
            buf.clear()

    while i < n:
        c = s[i]
        if c in stops:
            break
        if c == "'":                      # quoted literal ('' -> literal ')
            j, out = i + 1, []
            while j < n:
                if s[j] == "'":
                    if j + 1 < n and s[j + 1] == "'":
                        out.append("'"); j += 2; continue
                    j += 1; break
                out.append(s[j]); j += 1
            buf.append("".join(out)); i = j; continue
        if c == "%":
            j = s.find("%", i + 1)
            if j == -1:
                buf.append(c); i += 1; continue
            flush(); nodes.append(("field", s[i + 1:j])); i = j + 1; continue
        if c == "[":
            flush()
            child, i2 = _parse_seq(s, i + 1, "]")
            if i2 < n and s[i2] == "]":
                i2 += 1
            nodes.append(("opt", child)); i = i2; continue
        if c == "$":
            m, name = i + 1, []
            while m < n and (s[m].isalnum() or s[m] == "_"):
                name.append(s[m]); m += 1
            if m < n and s[m] == "(":
                flush(); m += 1; args = []
                while True:
                    arg, m = _parse_seq(s, m, ",)")
                    args.append(arg)
                    if m < n and s[m] == ",":
                        m += 1; continue
                    if m < n and s[m] == ")":
                        m += 1
                    break
                nodes.append(("func", "".join(name), args)); i = m; continue
            buf.append(c); i += 1; continue
        buf.append(c); i += 1
    flush()
    return nodes, i


def parse_pattern(s: str):
    nodes, _ = _parse_seq(s, 0, "")
    return nodes


def _eval_nodes(nodes, ctx, optional):
    parts, present_any = [], False
    for node in nodes:
        text, present = _eval_node(node, ctx, optional)
        parts.append(text)
        present_any = present_any or present
    return "".join(parts), present_any


def _eval_node(node, ctx, optional):
    kind = node[0]
    if kind == "lit":
        return node[1], False
    if kind == "field":
        name = node[1].lower()
        raw = _clean_value(_format_field(name, ctx["tags"], ctx["stem"]))
        if raw:
            return raw, True
        return ("" if optional else _FIELD_DEFAULTS.get(name, "")), False
    if kind == "opt":
        text, present = _eval_nodes(node[1], ctx, True)
        return (text, True) if present else ("", False)
    if kind == "func":
        return _eval_func(node[1], node[2], ctx, optional)
    return "", False


def _eval_func(name, args, ctx, optional):
    name = name.lower()

    # Fields inside function args never take their fallback defaults, so tests
    # like $if(%genre%,...) correctly see an absent tag as empty.
    def val(idx):
        if idx >= len(args):
            return "", False
        return _eval_nodes(args[idx], ctx, True)

    if name == "num":
        x, _ = val(0); nstr, _ = val(1)
        try:
            width = int(nstr.strip())
        except ValueError:
            width = 0
        xs = x.strip()
        if xs.isdigit():
            return xs.zfill(width), True
        if xs == "":
            return "0".zfill(width), False   # empty number -> zero-padded 0
        return xs, True                       # non-numeric passthrough
    if name == "if":
        cond, cp = val(0)
        return val(1) if (cond.strip() or cp) else val(2)
    if name == "if2":
        a, ap = val(0)
        return (a, ap) if (a.strip() or ap) else val(1)
    if name == "upper":
        x, p = val(0); return x.upper(), p
    if name == "lower":
        x, p = val(0); return x.lower(), p
    if name == "replace":
        x, p = val(0); a, _ = val(1); b, _ = val(2)
        return (x.replace(a, b) if a else x), p
    if name == "left":
        x, p = val(0); ns, _ = val(1)
        try:
            k = int(ns.strip())
        except ValueError:
            k = len(x)
        return x[:k], p
    return "", False


def render_pattern(pattern: str, tags: dict, stem_fallback: str) -> str:
    """Render a pattern into a relative path (no extension).

    Supports %fields%, [optional sections that vanish when their fields are
    empty], 'literal text', and $functions. Literal '/' separates folders;
    field values have their own separators sanitized so they can't escape a
    segment.
    """
    ctx = {"tags": tags, "stem": (stem_fallback or "").strip()}
    text, _ = _eval_nodes(parse_pattern(pattern), ctx, optional=False)
    text = text.replace("\\", "/")
    segments = [seg for seg in (_clean_segment(s) for s in text.split("/")) if seg]
    return "/".join(segments) if segments else "Untitled"


def build_organize_plan(tracks, mode, target_root, pattern):
    """Return a list of dicts: {src, dest, changed, conflict}.

    mode: 'move'/'copy' (into target_root/<pattern>) or 'rename' (pattern
    basename in the file's own folder). Conflicts = two sources → same dest, or
    dest already exists on disk as a different file.
    """
    entries = []
    for track in tracks:
        src = track["path"]
        ext = src.suffix.lower()
        if ext == ".jpeg":
            ext = ".jpg"
        rel = render_pattern(pattern, track["edited"], src.stem)
        if mode == "rename":
            name = rel.split("/")[-1]
            dest = src.parent / (name + ext)
        else:
            dest = Path(target_root) / (rel + ext)
        entries.append({"src": src, "dest": dest, "changed": dest != src,
                        "conflict": False})

    # Collisions: multiple sources targeting the same destination.
    counts = {}
    for e in entries:
        counts[str(e["dest"])] = counts.get(str(e["dest"]), 0) + 1
    src_set = {str(e["src"]) for e in entries}
    for e in entries:
        if not e["changed"]:
            continue
        if counts[str(e["dest"])] > 1:
            e["conflict"] = True
        elif e["dest"].exists() and str(e["dest"]) not in src_set:
            e["conflict"] = True  # a pre-existing, unrelated file is in the way
    return entries


# ── Duplicate detection ───────────────────────────────────────────────────────

DUP_MODES = [
    ("artist_title", "Same tags (Artist + Title)"),
    ("artist_title_album", "Same tags (Artist + Title + Album)"),
    ("content", "Identical file content"),
]


def dup_key(track: dict, mode: str):
    """Grouping key for a track, or None if it can't be grouped in this mode."""
    e = track["edited"]
    g = lambda k: (e.get(k) or "").strip().lower()
    if mode == "artist_title_album":
        title = g("title")
        return (g("artist") or g("albumartist"), title, g("album")) if title else None
    # default: artist + title
    title = g("title")
    return (g("artist") or g("albumartist"), title) if title else None


def file_hash(path, chunk=1 << 20) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def count_filled_tags(track: dict) -> int:
    return sum(1 for f in FIELDS if (track["edited"].get(f) or "").strip())


def pick_keeper(tracks: list, indices: list) -> int:
    """Choose which duplicate to keep: most tags, then largest file, then
    shortest path. Returns an index into `tracks`."""
    def score(i):
        t = tracks[i]
        try:
            size = t["path"].stat().st_size
        except OSError:
            size = 0
        return (count_filled_tags(t), size, -len(str(t["path"])))
    return max(indices, key=score)


def remove_empty_dirs(root: Path):
    """Delete empty directories under root (deepest first). Returns count."""
    removed = 0
    root = Path(root)
    if not root.exists():
        return 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        p = Path(dirpath)
        if p == root:
            continue
        try:
            if not any(p.iterdir()):
                p.rmdir()
                removed += 1
        except OSError:
            pass
    return removed


# ── Background workers ────────────────────────────────────────────────────────

class ScanWorker(QThread):
    progress = pyqtSignal(int, int)      # scanned, total
    finished = pyqtSignal(list, bool)    # tracks, was_cancelled
    error = pyqtSignal(str)

    def __init__(self, music_dir: Path, excluded_folders: set):
        super().__init__()
        self.music_dir = music_dir
        self.excluded_folders = excluded_folders
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            candidates = []
            for root, dirs, files in os.walk(self.music_dir):
                if self._stop:
                    self.finished.emit([], True)
                    return
                root_path = Path(root)
                dirs[:] = [
                    d for d in dirs
                    if not d.startswith('.')
                    and root_path / d not in self.excluded_folders
                ]
                for fname in files:
                    fpath = root_path / fname
                    if fpath.suffix.lower() in AUDIO_EXTENSIONS:
                        candidates.append(fpath)

            tracks = []
            for i, fpath in enumerate(candidates):
                if self._stop:
                    self.finished.emit(tracks, True)
                    return
                self.progress.emit(i + 1, len(candidates))
                tags = read_tags(fpath)
                if tags is None:
                    continue
                tracks.append({
                    'path': fpath,
                    'original': dict(tags),
                    'edited': dict(tags),
                    'art_action': None,      # None | ('remove',) | ('replace', data, mime)
                    'art_loaded': False,     # has art_original been read from disk yet?
                    'art_original': None,    # (data, mime) or None once loaded
                })

            self.finished.emit(tracks, False)
        except Exception as e:
            self.error.emit(str(e))


class ApplyWorker(QThread):
    """Writes a batch of jobs. Each job: (idx, path, tag_changes, art_action).

    If backup_root is set, each file is copied there (preserving its path
    relative to music_dir) before it's modified.
    """
    progress = pyqtSignal(int, int)
    finished = pyqtSignal(int, list)   # written_count, errors

    def __init__(self, jobs, music_dir=None, backup_root=None):
        super().__init__()
        self.jobs = jobs
        self.music_dir = music_dir
        self.backup_root = backup_root

    def run(self):
        errors = []
        written = 0
        for i, (idx, path, tag_changes, art_action) in enumerate(self.jobs):
            self.progress.emit(i + 1, len(self.jobs))
            try:
                if self.backup_root is not None:
                    try:
                        rel = Path(path).relative_to(self.music_dir)
                    except (ValueError, TypeError):
                        rel = Path(Path(path).name)
                    dest = Path(self.backup_root) / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, dest)
                if tag_changes:
                    write_tags(Path(path), tag_changes)
                if art_action is not None:
                    apply_art_action(Path(path), art_action)
                written += 1
            except Exception as e:
                errors.append((str(path), str(e)))
        self.finished.emit(written, errors)


class OrganizeWorker(QThread):
    """Moves/copies/renames files per a plan, then optionally removes empty dirs.

    plan: list of {src, dest, changed, conflict}. Only changed, non-conflict
    entries are executed. Emits tagged undo records:
      ('move', new_path, old_src)  — reverse by moving back
      ('delete', new_path, None)   — reverse by deleting the copy
    """
    progress = pyqtSignal(int, int)
    finished = pyqtSignal(int, list, list, int)  # done, errors, undo_records, dirs_removed

    def __init__(self, plan, mode="move", remove_empty=False, source_root=None):
        super().__init__()
        self.plan = plan
        self.mode = mode
        self.remove_empty = remove_empty
        self.source_root = source_root

    def run(self):
        errors = []
        undo_records = []
        done = 0
        is_copy = self.mode == "copy"
        todo = [e for e in self.plan if e["changed"] and not e["conflict"]]
        for i, e in enumerate(todo):
            self.progress.emit(i + 1, len(todo))
            src, dest = Path(e["src"]), Path(e["dest"])
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                final = dest
                if final.exists():
                    stem, suffix, n = final.stem, final.suffix, 1
                    while final.exists():
                        final = final.parent / f"{stem} ({n}){suffix}"
                        n += 1
                if is_copy:
                    shutil.copy2(str(src), str(final))
                    undo_records.append(("delete", str(final), None))
                else:
                    shutil.move(str(src), str(final))
                    undo_records.append(("move", str(final), str(src)))
                done += 1
            except Exception as ex:
                errors.append((str(src), str(ex)))

        dirs_removed = 0
        if not is_copy and self.remove_empty and self.source_root:
            dirs_removed = remove_empty_dirs(Path(self.source_root))
        self.finished.emit(done, errors, undo_records, dirs_removed)


class UndoWorker(QThread):
    """Reverses OrganizeWorker records: move-backs and copy-deletes."""
    progress = pyqtSignal(int, int)
    finished = pyqtSignal(int, list)   # restored, errors

    def __init__(self, records):
        super().__init__()
        self.records = records

    def run(self):
        errors = []
        restored = 0
        for i, rec in enumerate(self.records):
            self.progress.emit(i + 1, len(self.records))
            kind = rec[0]
            try:
                if kind == "move":
                    Path(rec[2]).parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(rec[1]), str(rec[2]))
                elif kind == "delete":
                    p = Path(rec[1])
                    if p.exists():
                        p.unlink()
                restored += 1
            except Exception as ex:
                errors.append((str(rec[1]), str(ex)))
        self.finished.emit(restored, errors)


class DuplicateScanWorker(QThread):
    """Groups tracks into duplicate sets by tags or by file content."""
    progress = pyqtSignal(int, int)
    finished = pyqtSignal(list)   # groups: list of [track_index, ...] with len > 1

    def __init__(self, tracks, mode):
        super().__init__()
        self.tracks = tracks
        self.mode = mode
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        groups = {}
        n = len(self.tracks)
        for i, t in enumerate(self.tracks):
            if self._stop:
                self.finished.emit([])
                return
            self.progress.emit(i + 1, n)
            if self.mode == "content":
                try:
                    key = file_hash(t["path"])
                except Exception:
                    continue
            else:
                key = dup_key(t, self.mode)
                if key is None:
                    continue
            groups.setdefault(key, []).append(i)
        dupes = [idxs for idxs in groups.values() if len(idxs) > 1]
        self.finished.emit(dupes)


# ── Tree branch arrow icons (generated at runtime, no asset files) ────────────

def _write_png_rgba(path, width, height, pixels):
    """pixels: flat list of (r,g,b,a) tuples, row-major."""
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0 per scanline
        for x in range(width):
            raw.extend(pixels[y * width + x])

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + chunk(b"IEND", b""))
    Path(path).write_bytes(png)


def _triangle_png(path, verts, size=14, color=(183, 181, 172, 255)):
    """Draw a filled triangle (3 (x,y) verts) on a transparent square."""
    (ax, ay), (bx, by), (cx, cy) = verts

    def sign(px, py, x1, y1, x2, y2):
        return (px - x2) * (y1 - y2) - (x1 - x2) * (py - y2)

    pixels = []
    for y in range(size):
        for x in range(size):
            px, py = x + 0.5, y + 0.5
            d1 = sign(px, py, ax, ay, bx, by)
            d2 = sign(px, py, bx, by, cx, cy)
            d3 = sign(px, py, cx, cy, ax, ay)
            has_neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
            has_pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
            inside = not (has_neg and has_pos)
            pixels.append(color if inside else (0, 0, 0, 0))
    _write_png_rgba(path, size, size, pixels)


def generate_tree_icons(out_dir: Path):
    """Create closed (▸) and open (▾) arrow PNGs; return (closed_path, open_path)."""
    s = 14
    closed = out_dir / "branch-closed.png"
    open_ = out_dir / "branch-open.png"
    _triangle_png(closed, [(4, 3), (4, s - 3), (s - 4, s / 2)], size=s)   # points right
    _triangle_png(open_, [(3, 4), (s - 3, 4), (s / 2, s - 4)], size=s)    # points down
    return closed, open_


def tree_icon_stylesheet(closed_path: Path, open_path: Path) -> str:
    c = str(closed_path).replace("\\", "/")
    o = str(open_path).replace("\\", "/")
    return (
        "QTreeWidget::branch:has-children:closed,"
        "QTreeWidget::branch:closed:has-children:has-siblings {"
        f' image: url("{c}"); }}\n'
        "QTreeWidget::branch:open:has-children,"
        "QTreeWidget::branch:open:has-children:has-siblings {"
        f' image: url("{o}"); }}\n'
    )


# ── Mount dialog (Linux NAS) ──────────────────────────────────────────────────

class MountDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Mount NAS Share")
        self.setMinimumWidth(420)
        self.mounted_path = None

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        form_layout = QVBoxLayout()

        def row(label, widget):
            h = QHBoxLayout()
            lbl = QLabel(label); lbl.setFixedWidth(110)
            h.addWidget(lbl); h.addWidget(widget)
            form_layout.addLayout(h)

        self.host = QLineEdit("truenas.local")
        self.share = QLineEdit("music")
        self.username = QLineEdit("guy")
        self.password = QLineEdit(); self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.mountpoint = QLineEdit("/mnt/nas-music")
        self.sudo_password = QLineEdit(); self.sudo_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.sudo_password.setPlaceholderText("Required to run mount.cifs")

        row("NAS Host:", self.host)
        row("Share Path:", self.share)
        row("Username:", self.username)
        row("NAS Password:", self.password)
        row("Mount Point:", self.mountpoint)
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        form_layout.addWidget(sep)
        row("Sudo Password:", self.sudo_password)
        layout.addLayout(form_layout)

        self.status = QLabel(""); self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QDialogButtonBox()
        self.mount_btn = QPushButton("Mount"); self.mount_btn.setDefault(True)
        cancel_btn = QPushButton("Cancel")
        buttons.addButton(self.mount_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(cancel_btn, QDialogButtonBox.ButtonRole.RejectRole)
        self.mount_btn.clicked.connect(self._do_mount)
        cancel_btn.clicked.connect(self.reject)
        layout.addWidget(buttons)

    def _do_mount(self):
        self.mount_btn.setEnabled(False)
        self.status.setText("Mounting…")
        QApplication.processEvents()
        mount_point = Path(self.mountpoint.text().strip())
        smb_path = f"//{self.host.text().strip()}/{self.share.text().strip()}"
        username = self.username.text().strip()
        password = self.password.text()
        uid, gid = os.getuid(), os.getgid()
        try:
            mount_point.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.status.setText(f"Could not create mount point: {e}")
            self.mount_btn.setEnabled(True)
            return
        cmd = [
            'sudo', '-S', 'mount.cifs', smb_path, str(mount_point),
            '-o', f'username={username},password={password},uid={uid},gid={gid},iocharset=utf8',
        ]
        result = subprocess.run(cmd, input=self.sudo_password.text() + '\n',
                                capture_output=True, text=True)
        if result.returncode == 0:
            self.mounted_path = str(mount_point)
            self.accept()
        else:
            msg = result.stderr.strip() or "mount.cifs failed (check sudo permissions)"
            self.status.setText(f"Error: {msg}")
            self.mount_btn.setEnabled(True)


# ── Main window ───────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    CHANGED_BG = QColor("#1e1b26")
    CHANGED_FG = QColor("#8b7ec8")   # purple 400
    INVALID_BG = QColor("#2a1714")
    INVALID_FG = QColor("#d14d41")   # red 400

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Music Organizer")
        self.setMinimumSize(1200, 700)
        self.music_dir: Optional[Path] = None
        self.tracks: list = []
        self._scan_worker: Optional[ScanWorker] = None
        self._apply_worker: Optional[ApplyWorker] = None
        self._undo_worker: Optional[ApplyWorker] = None
        self._mounted_path: Optional[Path] = None

        self._updating_checks = False
        self._table_updating = False
        self._root_excluded = False

        self._apply_jobs = []
        self._undo_records = []      # snapshots to restore on undo
        self._last_backup_root = None

        # Organize tab state
        self._organize_worker: Optional[OrganizeWorker] = None
        self._org_undo_worker: Optional[UndoWorker] = None
        self._organize_plan = []
        self._org_undo_records = []

        # Duplicates tab state
        self._dup_worker: Optional[DuplicateScanWorker] = None
        self._dup_move_worker: Optional[OrganizeWorker] = None
        self._dup_undo_worker: Optional[UndoWorker] = None
        self._dup_groups = []            # [{'indices':[...], 'keep': idx}]
        self._dup_undo_records = []

        self._build_ui()
        self._set_controls_enabled(False)

        if not MUTAGEN_AVAILABLE:
            QMessageBox.critical(
                self, "Missing dependency",
                "The 'mutagen' library is not installed.\n\n"
                "Install it with:  pip install mutagen",
            )

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setSpacing(8)
        outer.setContentsMargins(10, 10, 10, 10)

        # Shared source bar — visible above every tab so the folder can be
        # changed or cleared at any time, from any tab.
        outer.addWidget(self._build_source_bar())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_tags_tab(), "Tags")
        self.tabs.addTab(self._build_organize_tab(), "Organize")
        self.tabs.addTab(self._build_duplicates_tab(), "Duplicates")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        outer.addWidget(self.tabs, stretch=1)
        self.statusBar().showMessage("Select a folder to get started.")

    def _build_source_bar(self):
        source_group = QGroupBox("Source Directory")
        source_layout = QHBoxLayout(source_group)
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("Select a music folder or mount the NAS…")
        self.path_edit.setReadOnly(True)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setToolTip("Forget the current folder and reset all tabs")
        self.clear_btn.clicked.connect(self._clear_source)
        self.clear_btn.setEnabled(False)
        source_layout.addWidget(self.path_edit)
        source_layout.addWidget(browse_btn)
        source_layout.addWidget(self.clear_btn)
        if sys.platform.startswith("linux"):
            self.mount_btn = QPushButton("Mount NAS…"); self.mount_btn.clicked.connect(self._mount_nas)
            self.unmount_btn = QPushButton("Unmount"); self.unmount_btn.clicked.connect(self._unmount_nas)
            self.unmount_btn.setVisible(False)
            source_layout.addWidget(self.mount_btn)
            source_layout.addWidget(self.unmount_btn)
        else:
            self.mount_btn = QPushButton(); self.mount_btn.hide()
            self.unmount_btn = QPushButton(); self.unmount_btn.hide()
        return source_group

    def _build_tags_tab(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setSpacing(8)
        root.setContentsMargins(10, 10, 10, 10)

        # Bulk-edit row
        bulk_group = QGroupBox("Bulk Edit  —  set a field on all selected rows")
        bulk_layout = QHBoxLayout(bulk_group)
        bulk_layout.addWidget(QLabel("Field:"))
        self.bulk_field = QComboBox()
        for key, header in EDITABLE_HEADERS:
            self.bulk_field.addItem(header, key)
        self.bulk_field.setFixedWidth(160)
        bulk_layout.addWidget(self.bulk_field)
        bulk_layout.addWidget(QLabel("Value:"))
        self.bulk_value = QLineEdit()
        self.bulk_value.setPlaceholderText("Value to apply (leave blank to clear the field)")
        self.bulk_value.returnPressed.connect(self._apply_bulk)
        bulk_layout.addWidget(self.bulk_value)
        self.bulk_btn = QPushButton("Apply to Selected")
        self.bulk_btn.clicked.connect(self._apply_bulk)
        bulk_layout.addWidget(self.bulk_btn)
        root.addWidget(bulk_group)

        # Splitter: folder tree | table | art panel
        splitter = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("Folders to include:"))
        folder_btn_row = QHBoxLayout()
        folder_btn_row.setContentsMargins(0, 0, 0, 0)
        folder_btn_row.addStretch()
        all_btn = QPushButton("All"); all_btn.setFixedWidth(56)
        all_btn.clicked.connect(lambda: self._set_all_folders(True))
        none_btn = QPushButton("None"); none_btn.setFixedWidth(64)
        none_btn.clicked.connect(lambda: self._set_all_folders(False))
        folder_btn_row.addWidget(all_btn); folder_btn_row.addWidget(none_btn)
        left_layout.addLayout(folder_btn_row)
        self.folder_tree = QTreeWidget()
        self.folder_tree.setHeaderHidden(True)
        self.folder_tree.itemChanged.connect(self._on_folder_check_changed)
        left_layout.addWidget(self.folder_tree)
        scan_row = QHBoxLayout()
        self.scan_btn = QPushButton("Scan Music"); self.scan_btn.setFixedHeight(34)
        self.scan_btn.clicked.connect(self._start_scan)
        self.stop_scan_btn = QPushButton("Stop Scanning"); self.stop_scan_btn.setFixedHeight(34)
        self.stop_scan_btn.setObjectName("stopBtn"); self.stop_scan_btn.setVisible(False)
        self.stop_scan_btn.clicked.connect(self._stop_scan)
        scan_row.addWidget(self.scan_btn); scan_row.addWidget(self.stop_scan_btn)
        left_layout.addLayout(scan_row)
        left.setMinimumWidth(200); left.setMaximumWidth(320)
        splitter.addWidget(left)

        # Middle: table
        middle = QWidget()
        middle_layout = QVBoxLayout(middle)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        preview_header = QHBoxLayout()
        preview_header.addWidget(QLabel("Tracks:"))
        preview_header.addStretch()
        self.stats_label = QLabel(""); self.stats_label.setObjectName("statsLabel")
        preview_header.addWidget(self.stats_label)
        middle_layout.addLayout(preview_header)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels([h for _, h in COLUMNS])
        hh = self.table.horizontalHeader()
        for c in range(len(COLUMNS)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 180)
        for c in range(1, len(COLUMNS)):
            self.table.setColumnWidth(c, 120)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.itemChanged.connect(self._on_table_item_changed)
        self.table.itemSelectionChanged.connect(self._update_art_panel)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_context_menu)
        middle_layout.addWidget(self.table)
        splitter.addWidget(middle)

        # Right: cover art panel
        art = QWidget()
        art_layout = QVBoxLayout(art)
        art_layout.setContentsMargins(6, 0, 0, 0)
        art_layout.addWidget(QLabel("Cover Art:"))
        self.art_view = QLabel("Select a track")
        self.art_view.setObjectName("artView")
        self.art_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.art_view.setFixedSize(200, 200)
        self.art_view.setWordWrap(True)
        art_layout.addWidget(self.art_view)
        self.art_info = QLabel("")
        self.art_info.setObjectName("statsLabel")
        self.art_info.setWordWrap(True)
        art_layout.addWidget(self.art_info)
        art_btns = QHBoxLayout()
        self.art_replace_btn = QPushButton("Replace…")
        self.art_replace_btn.clicked.connect(self._replace_art)
        self.art_remove_btn = QPushButton("Remove")
        self.art_remove_btn.clicked.connect(self._remove_art)
        art_btns.addWidget(self.art_replace_btn)
        art_btns.addWidget(self.art_remove_btn)
        art_layout.addLayout(art_btns)
        note = QLabel("Applies to the selected track. Supported: MP3, FLAC, M4A, OGG/Opus.")
        note.setObjectName("statsLabel"); note.setWordWrap(True)
        art_layout.addWidget(note)
        art_layout.addStretch()
        art.setMinimumWidth(220); art.setMaximumWidth(260)
        splitter.addWidget(art)

        splitter.setSizes([240, 760, 230])
        root.addWidget(splitter, stretch=1)

        # Bottom bar
        bottom = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setVisible(False); self.progress.setFixedHeight(18)
        bottom.addWidget(self.progress)
        bottom.addStretch()
        self.backup_chk = QCheckBox("Back up originals before writing")
        self.backup_chk.setChecked(True)
        self.backup_chk.setToolTip("Copy each modified file to a timestamped backup "
                                   "folder before writing tags")
        bottom.addWidget(self.backup_chk)
        self.undo_btn = QPushButton("Undo Last Apply")
        self.undo_btn.setFixedHeight(38)
        self.undo_btn.setEnabled(False)
        self.undo_btn.clicked.connect(self._undo_last_apply)
        bottom.addWidget(self.undo_btn)
        self.apply_btn = QPushButton("Apply Changes")
        self.apply_btn.setFixedHeight(38); self.apply_btn.setFixedWidth(160)
        self.apply_btn.setObjectName("applyBtn")
        self.apply_btn.clicked.connect(self._confirm_and_apply)
        bottom.addWidget(self.apply_btn)
        root.addLayout(bottom)

        self._reset_art_panel()
        return central

    # ── Organize tab ──────────────────────────────────────────────────────────

    def _build_organize_tab(self):
        page = QWidget()
        root = QVBoxLayout(page)
        root.setSpacing(8)
        root.setContentsMargins(10, 10, 10, 10)

        opts = QGroupBox("File Operation")
        opts_lay = QVBoxLayout(opts)

        # Mode + target row
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Operation:"))
        self.org_mode = QComboBox()
        self.org_mode.addItem("Move into folder structure", "move")
        self.org_mode.addItem("Copy into folder structure", "copy")
        self.org_mode.addItem("Rename in place", "rename")
        self.org_mode.setFixedWidth(220)
        self.org_mode.currentIndexChanged.connect(self._on_org_mode_changed)
        row1.addWidget(self.org_mode)
        row1.addSpacing(16)
        self.org_target_label = QLabel("Destination:")
        row1.addWidget(self.org_target_label)
        self.org_target_edit = QLineEdit()
        self.org_target_edit.setPlaceholderText("Destination root folder for the new structure")
        self.org_target_edit.setReadOnly(True)
        row1.addWidget(self.org_target_edit)
        self.org_target_btn = QPushButton("Browse…")
        self.org_target_btn.clicked.connect(self._browse_org_target)
        row1.addWidget(self.org_target_btn)
        opts_lay.addLayout(row1)

        # Pattern row
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Pattern:"))
        self.org_pattern = QLineEdit(DEFAULT_PATTERN)
        self.org_pattern.textChanged.connect(self._rebuild_organize_preview)
        row2.addWidget(self.org_pattern)
        opts_lay.addLayout(row2)

        tokens = QLabel(
            "Tokens: " + "  ".join(PATTERN_TOKENS)
            + "\nFunctions: " + "  ".join(PATTERN_FUNCS)
            + "\n'/' starts a subfolder · [ … ] is dropped when its fields are empty"
            " · 'text' is literal · the file extension is kept automatically.")
        tokens.setObjectName("statsLabel")
        tokens.setWordWrap(True)
        opts_lay.addWidget(tokens)

        row3 = QHBoxLayout()
        self.org_remove_empty = QCheckBox("Remove empty source folders after moving")
        self.org_remove_empty.setChecked(True)
        row3.addWidget(self.org_remove_empty)
        row3.addStretch()
        opts_lay.addLayout(row3)
        root.addWidget(opts)

        # Preview
        header = QHBoxLayout()
        header.addWidget(QLabel("Preview:"))
        header.addStretch()
        self.org_stats = QLabel(""); self.org_stats.setObjectName("statsLabel")
        header.addWidget(self.org_stats)
        root.addLayout(header)

        self.org_table = QTableWidget(0, 2)
        self.org_table.setHorizontalHeaderLabels(["Current", "New path"])
        ohh = self.org_table.horizontalHeader()
        ohh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        ohh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.org_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.org_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.org_table.setAlternatingRowColors(True)
        self.org_table.verticalHeader().setVisible(False)
        root.addWidget(self.org_table, stretch=1)

        # Bottom bar
        bottom = QHBoxLayout()
        self.org_progress = QProgressBar()
        self.org_progress.setVisible(False); self.org_progress.setFixedHeight(18)
        bottom.addWidget(self.org_progress)
        bottom.addStretch()
        self.org_undo_btn = QPushButton("Undo Last Organize")
        self.org_undo_btn.setFixedHeight(38)
        self.org_undo_btn.setEnabled(False)
        self.org_undo_btn.clicked.connect(self._undo_last_organize)
        bottom.addWidget(self.org_undo_btn)
        self.org_run_btn = QPushButton("Organize Files")
        self.org_run_btn.setFixedHeight(38); self.org_run_btn.setFixedWidth(160)
        self.org_run_btn.setObjectName("applyBtn")
        self.org_run_btn.setEnabled(False)
        self.org_run_btn.clicked.connect(self._confirm_and_organize)
        bottom.addWidget(self.org_run_btn)
        root.addLayout(bottom)

        self._on_org_mode_changed()
        return page

    def _on_tab_changed(self, index):
        if self.tabs.tabText(index) == "Organize":
            if not self.org_target_edit.text() and self.music_dir:
                self.org_target_edit.setText(str(self.music_dir))
            self._rebuild_organize_preview()

    def _on_org_mode_changed(self, *_):
        mode = self.org_mode.currentData()
        needs_target = mode in ("move", "copy")
        self.org_target_label.setVisible(needs_target)
        self.org_target_edit.setVisible(needs_target)
        self.org_target_btn.setVisible(needs_target)
        self.org_remove_empty.setEnabled(mode == "move")
        self._rebuild_organize_preview()

    def _browse_org_target(self):
        start = self.org_target_edit.text() or (str(self.music_dir) if self.music_dir else "")
        path = QFileDialog.getExistingDirectory(self, "Select Destination Folder", start)
        if path:
            self.org_target_edit.setText(path)
            self._rebuild_organize_preview()

    def _rebuild_organize_preview(self, *_):
        if not hasattr(self, "org_table"):
            return
        mode = self.org_mode.currentData()
        target = self.org_target_edit.text().strip()
        pattern = self.org_pattern.text().strip() or DEFAULT_PATTERN

        if not self.tracks:
            self.org_table.setRowCount(0)
            self.org_stats.setText("Scan a folder on the Tags tab first.")
            self.org_run_btn.setEnabled(False)
            return
        if mode in ("move", "copy") and not target:
            self.org_table.setRowCount(0)
            self.org_stats.setText("Choose a destination folder.")
            self.org_run_btn.setEnabled(False)
            return

        self._organize_plan = build_organize_plan(self.tracks, mode, target, pattern)
        conflict_bg, conflict_fg = QColor("#2a1714"), QColor("#d14d41")
        change_fg, noop_fg = QColor("#cecdc3"), QColor("#6f6e69")

        self.org_table.setRowCount(0)
        changed = conflicts = 0
        for e in self._organize_plan:
            row = self.org_table.rowCount()
            self.org_table.insertRow(row)
            try:
                cur = str(e["src"].relative_to(self.music_dir)) if self.music_dir else str(e["src"])
            except ValueError:
                cur = str(e["src"])
            if e["conflict"]:
                new_disp = str(e["dest"]) + "   ⚠ conflict"
            elif not e["changed"]:
                new_disp = "(no change)"
            else:
                try:
                    new_disp = (str(e["dest"].relative_to(target))
                                if mode in ("move", "copy") else e["dest"].name)
                except (ValueError, TypeError):
                    new_disp = str(e["dest"])
            cur_item = QTableWidgetItem(cur)
            new_item = QTableWidgetItem(new_disp)
            for it in (cur_item, new_item):
                if e["conflict"]:
                    it.setBackground(QBrush(conflict_bg)); it.setForeground(QBrush(conflict_fg))
                elif not e["changed"]:
                    it.setForeground(QBrush(noop_fg))
                else:
                    it.setForeground(QBrush(change_fg))
            self.org_table.setItem(row, 0, cur_item)
            self.org_table.setItem(row, 1, new_item)
            if e["conflict"]:
                conflicts += 1
            elif e["changed"]:
                changed += 1

        verb = {"rename": "rename", "copy": "copy"}.get(mode, "move")
        stat = f"{changed} to {verb}  |  {len(self._organize_plan) - changed - conflicts} unchanged"
        if conflicts:
            stat += f"  |  ⚠ {conflicts} conflict(s)"
        self.org_stats.setText(stat)
        self.org_run_btn.setEnabled(changed > 0 and conflicts == 0)
        self.org_run_btn.setToolTip("Resolve conflicts (red rows) before organizing" if conflicts else "")

    def _confirm_and_organize(self):
        todo = [e for e in self._organize_plan if e["changed"] and not e["conflict"]]
        if not todo:
            return
        mode = self.org_mode.currentData()
        verb = {"rename": "rename", "copy": "copy"}.get(mode, "move")
        msg = (f"{verb.capitalize()} {len(todo)} file(s) using the pattern?\n\n"
               "This changes files on disk. You can undo this "
               "immediately afterward with 'Undo Last Organize'.\n\nContinue?")
        reply = QMessageBox.question(
            self, "Confirm Organize", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return

        remove_empty = mode == "move" and self.org_remove_empty.isChecked()
        self.org_run_btn.setEnabled(False)
        self.org_undo_btn.setEnabled(False)
        self.org_progress.setVisible(True); self.org_progress.setValue(0)
        self.statusBar().showMessage(f"{verb.capitalize()}ing files…")

        self._organize_worker = OrganizeWorker(
            self._organize_plan, mode=mode, remove_empty=remove_empty,
            source_root=self.music_dir)
        self._organize_worker.progress.connect(self._on_org_progress)
        self._organize_worker.finished.connect(self._on_organize_finished)
        self._organize_worker.start()

    def _on_org_progress(self, done, total):
        self.org_progress.setMaximum(total)
        self.org_progress.setValue(done)

    def _on_organize_finished(self, done, errors, undo_records, dirs_removed):
        self.org_progress.setVisible(False)
        self._org_undo_records = undo_records
        self.org_undo_btn.setEnabled(bool(undo_records))

        # For moves/renames, update in-memory paths to their new homes.
        # (Copy leaves originals in place, so paths don't change.)
        moved_map = {rec[2]: rec[1] for rec in undo_records if rec[0] == "move"}
        for track in self.tracks:
            newp = moved_map.get(str(track["path"]))
            if newp:
                track["path"] = Path(newp)
        self._populate_table()          # refresh Tags tab filenames
        self._rebuild_organize_preview()

        extra = f"\nRemoved {dirs_removed} empty folder(s)." if dirs_removed else ""
        if errors:
            et = "\n".join(f"  {p}: {m}" for p, m in errors[:20])
            QMessageBox.warning(self, "Organize done with errors",
                                f"Processed {done} file(s).\n\n{len(errors)} error(s):\n{et}{extra}")
        else:
            QMessageBox.information(self, "Organize complete",
                                    f"Successfully processed {done} file(s).{extra}")
        self.statusBar().showMessage(f"Organize complete — {done} file(s).")

    def _undo_last_organize(self):
        if not self._org_undo_records:
            return
        is_copy = self._org_undo_records[0][0] == "delete"
        prompt = (f"Delete the {len(self._org_undo_records)} copied file(s)?"
                  if is_copy else
                  f"Move {len(self._org_undo_records)} file(s) back to their previous locations?")
        reply = QMessageBox.question(
            self, "Undo Last Organize", prompt,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.org_undo_btn.setEnabled(False)
        self.org_run_btn.setEnabled(False)
        self.org_progress.setVisible(True); self.org_progress.setValue(0)
        self.statusBar().showMessage("Undoing organize…")
        self._org_undo_worker = UndoWorker(list(self._org_undo_records))
        self._org_undo_worker.progress.connect(self._on_org_progress)
        self._org_undo_worker.finished.connect(self._on_org_undo_finished)
        self._org_undo_worker.start()

    def _on_org_undo_finished(self, restored, errors):
        self.org_progress.setVisible(False)
        back_map = {rec[1]: rec[2] for rec in self._org_undo_records if rec[0] == "move"}
        for track in self.tracks:
            oldp = back_map.get(str(track["path"]))
            if oldp:
                track["path"] = Path(oldp)
        self._org_undo_records = []
        self._populate_table()
        self._rebuild_organize_preview()
        if errors:
            et = "\n".join(f"  {p}: {m}" for p, m in errors[:20])
            QMessageBox.warning(self, "Undo done with errors",
                                f"Restored {restored} file(s).\n\n{len(errors)} error(s):\n{et}")
        else:
            QMessageBox.information(self, "Undo complete",
                                    f"Moved {restored} file(s) back.")
        self.statusBar().showMessage(f"Organize undo complete — {restored} file(s).")

    # ── Duplicates tab ────────────────────────────────────────────────────────

    KEEP_FG = QColor("#879a39")      # green 600-ish
    DUP_FG = QColor("#d14d41")       # red 400

    def _build_duplicates_tab(self):
        page = QWidget()
        root = QVBoxLayout(page)
        root.setSpacing(8)
        root.setContentsMargins(10, 10, 10, 10)

        find = QGroupBox("Find Duplicates")
        find_lay = QHBoxLayout(find)
        find_lay.addWidget(QLabel("Match by:"))
        self.dup_mode = QComboBox()
        for key, label in DUP_MODES:
            self.dup_mode.addItem(label, key)
        self.dup_mode.setFixedWidth(260)
        find_lay.addWidget(self.dup_mode)
        self.dup_find_btn = QPushButton("Find Duplicates")
        self.dup_find_btn.clicked.connect(self._start_dup_scan)
        find_lay.addWidget(self.dup_find_btn)
        self.dup_keeper_btn = QPushButton("Keep Selected Instead")
        self.dup_keeper_btn.setToolTip("Make the selected row the one kept in its group")
        self.dup_keeper_btn.clicked.connect(self._set_selected_keeper)
        self.dup_keeper_btn.setEnabled(False)
        find_lay.addWidget(self.dup_keeper_btn)
        find_lay.addStretch()
        self.dup_stats = QLabel(""); self.dup_stats.setObjectName("statsLabel")
        find_lay.addWidget(self.dup_stats)
        root.addWidget(find)

        self.dup_table = QTableWidget(0, 6)
        self.dup_table.setHorizontalHeaderLabels(
            ["Group", "Action", "File", "Artist", "Title", "Album"])
        dhh = self.dup_table.horizontalHeader()
        dhh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        dhh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        dhh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for c in (3, 4, 5):
            dhh.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
            self.dup_table.setColumnWidth(c, 150)
        self.dup_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.dup_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.dup_table.setAlternatingRowColors(True)
        self.dup_table.verticalHeader().setVisible(False)
        root.addWidget(self.dup_table, stretch=1)

        dest_row = QHBoxLayout()
        dest_row.addWidget(QLabel("Move duplicates to:"))
        self.dup_quarantine = QLineEdit()
        self.dup_quarantine.setPlaceholderText("Quarantine folder for the duplicates")
        self.dup_quarantine.setReadOnly(True)
        dest_row.addWidget(self.dup_quarantine)
        dup_browse = QPushButton("Browse…")
        dup_browse.clicked.connect(self._browse_quarantine)
        dest_row.addWidget(dup_browse)
        root.addLayout(dest_row)

        bottom = QHBoxLayout()
        self.dup_progress = QProgressBar()
        self.dup_progress.setVisible(False); self.dup_progress.setFixedHeight(18)
        bottom.addWidget(self.dup_progress)
        bottom.addStretch()
        self.dup_undo_btn = QPushButton("Undo Last")
        self.dup_undo_btn.setFixedHeight(38); self.dup_undo_btn.setEnabled(False)
        self.dup_undo_btn.clicked.connect(self._undo_dup_move)
        bottom.addWidget(self.dup_undo_btn)
        self.dup_move_btn = QPushButton("Quarantine Duplicates")
        self.dup_move_btn.setFixedHeight(38); self.dup_move_btn.setFixedWidth(200)
        self.dup_move_btn.setObjectName("applyBtn")
        self.dup_move_btn.setEnabled(False)
        self.dup_move_btn.clicked.connect(self._confirm_and_quarantine)
        bottom.addWidget(self.dup_move_btn)
        root.addLayout(bottom)

        note = QLabel("Duplicates are moved to a quarantine folder (not deleted) so you "
                      "can review before removing them. Undo moves them back.")
        note.setObjectName("statsLabel"); note.setWordWrap(True)
        root.addWidget(note)
        return page

    def _start_dup_scan(self):
        if not self.tracks:
            QMessageBox.information(self, "No tracks",
                                    "Scan a folder on the Tags tab first.")
            return
        mode = self.dup_mode.currentData()
        self.dup_find_btn.setEnabled(False)
        self.dup_move_btn.setEnabled(False)
        self.dup_keeper_btn.setEnabled(False)
        self.dup_progress.setVisible(True); self.dup_progress.setValue(0)
        self.statusBar().showMessage("Scanning for duplicates…")
        self._dup_worker = DuplicateScanWorker(self.tracks, mode)
        self._dup_worker.progress.connect(self._on_dup_progress)
        self._dup_worker.finished.connect(self._on_dup_scan_finished)
        self._dup_worker.start()

    def _on_dup_progress(self, done, total):
        self.dup_progress.setMaximum(total)
        self.dup_progress.setValue(done)

    def _on_dup_scan_finished(self, groups):
        self.dup_progress.setVisible(False)
        self.dup_find_btn.setEnabled(True)
        self._dup_groups = [{"indices": g, "keep": pick_keeper(self.tracks, g)}
                            for g in groups]
        if self.music_dir and not self.dup_quarantine.text():
            self.dup_quarantine.setText(str(self.music_dir / "MusicOrganizer-Duplicates"))
        self._populate_dup_table()
        dup_count = sum(len(g["indices"]) - 1 for g in self._dup_groups)
        self.statusBar().showMessage(
            f"Found {len(self._dup_groups)} duplicate group(s), {dup_count} removable file(s).")

    def _populate_dup_table(self):
        self.dup_table.setRowCount(0)
        self._dup_row_map = []   # row -> (group_i, track_idx)
        shade = QColor("#161514")
        removable = 0
        for gi, group in enumerate(self._dup_groups):
            keep = group["keep"]
            for idx in group["indices"]:
                row = self.dup_table.rowCount()
                self.dup_table.insertRow(row)
                self._dup_row_map.append((gi, idx))
                t = self.tracks[idx]
                e = t["edited"]
                is_keep = idx == keep
                if not is_keep:
                    removable += 1
                try:
                    fdisp = str(t["path"].relative_to(self.music_dir)) if self.music_dir else t["path"].name
                except ValueError:
                    fdisp = str(t["path"])
                cells = [str(gi + 1),
                         "KEEP" if is_keep else "→ quarantine",
                         fdisp, e.get("artist", ""), e.get("title", ""), e.get("album", "")]
                for c, text in enumerate(cells):
                    item = QTableWidgetItem(text)
                    if gi % 2 == 1:
                        item.setBackground(QBrush(shade))
                    if c == 1:
                        item.setForeground(QBrush(self.KEEP_FG if is_keep else self.DUP_FG))
                    self.dup_table.setItem(row, c, item)

        self.dup_stats.setText(
            f"{len(self._dup_groups)} group(s)  |  {removable} to quarantine"
            if self._dup_groups else "No duplicates found.")
        self.dup_move_btn.setEnabled(removable > 0 and bool(self.dup_quarantine.text()))
        self.dup_keeper_btn.setEnabled(bool(self._dup_groups))

    def _set_selected_keeper(self):
        rows = self.dup_table.selectionModel().selectedRows()
        if not rows:
            return
        gi, idx = self._dup_row_map[rows[0].row()]
        self._dup_groups[gi]["keep"] = idx
        self._populate_dup_table()

    def _browse_quarantine(self):
        start = self.dup_quarantine.text() or (str(self.music_dir) if self.music_dir else "")
        path = QFileDialog.getExistingDirectory(self, "Select Quarantine Folder", start)
        if path:
            self.dup_quarantine.setText(path)
            self.dup_move_btn.setEnabled(
                any(len(g["indices"]) > 1 for g in self._dup_groups))

    def _confirm_and_quarantine(self):
        quarantine = self.dup_quarantine.text().strip()
        if not quarantine:
            return
        plan = []
        for group in self._dup_groups:
            for idx in group["indices"]:
                if idx == group["keep"]:
                    continue
                src = self.tracks[idx]["path"]
                try:
                    rel = src.relative_to(self.music_dir)
                except (ValueError, TypeError):
                    rel = Path(src.name)
                plan.append({"src": src, "dest": Path(quarantine) / rel,
                             "changed": True, "conflict": False})
        if not plan:
            return
        reply = QMessageBox.question(
            self, "Quarantine Duplicates",
            f"Move {len(plan)} duplicate file(s) into:\n\n  {quarantine}\n\n"
            "The chosen keeper in each group stays put. Undo moves them back. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.dup_move_btn.setEnabled(False)
        self.dup_undo_btn.setEnabled(False)
        self.dup_find_btn.setEnabled(False)
        self.dup_progress.setVisible(True); self.dup_progress.setValue(0)
        self.statusBar().showMessage("Quarantining duplicates…")
        self._dup_move_worker = OrganizeWorker(plan, mode="move")
        self._dup_move_worker.progress.connect(self._on_dup_progress)
        self._dup_move_worker.finished.connect(self._on_dup_move_finished)
        self._dup_move_worker.start()

    def _on_dup_move_finished(self, moved, errors, undo_records, dirs_removed):
        self.dup_progress.setVisible(False)
        self.dup_find_btn.setEnabled(True)
        self._dup_undo_records = undo_records
        self.dup_undo_btn.setEnabled(bool(undo_records))

        moved_map = {rec[2]: rec[1] for rec in undo_records if rec[0] == "move"}
        for track in self.tracks:
            newp = moved_map.get(str(track["path"]))
            if newp:
                track["path"] = Path(newp)
        # Clear the resolved groups from view.
        self._dup_groups = []
        self._populate_dup_table()
        self._populate_table()

        if errors:
            et = "\n".join(f"  {p}: {m}" for p, m in errors[:20])
            QMessageBox.warning(self, "Quarantine done with errors",
                                f"Moved {moved} file(s).\n\n{len(errors)} error(s):\n{et}")
        else:
            QMessageBox.information(self, "Duplicates quarantined",
                                    f"Moved {moved} duplicate file(s) to the quarantine folder.")
        self.statusBar().showMessage(f"Quarantined {moved} duplicate(s).")

    def _undo_dup_move(self):
        if not self._dup_undo_records:
            return
        self.dup_undo_btn.setEnabled(False)
        self.dup_progress.setVisible(True); self.dup_progress.setValue(0)
        self.statusBar().showMessage("Undoing quarantine…")
        self._dup_undo_worker = UndoWorker(list(self._dup_undo_records))
        self._dup_undo_worker.progress.connect(self._on_dup_progress)
        self._dup_undo_worker.finished.connect(self._on_dup_undo_finished)
        self._dup_undo_worker.start()

    def _on_dup_undo_finished(self, restored, errors):
        self.dup_progress.setVisible(False)
        back_map = {rec[1]: rec[2] for rec in self._dup_undo_records if rec[0] == "move"}
        for track in self.tracks:
            oldp = back_map.get(str(track["path"]))
            if oldp:
                track["path"] = Path(oldp)
        self._dup_undo_records = []
        self._populate_table()
        QMessageBox.information(self, "Undo complete",
                                f"Moved {restored} file(s) back.")
        self.statusBar().showMessage(f"Duplicate undo complete — {restored} file(s).")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_controls_enabled(self, enabled: bool):
        self.scan_btn.setEnabled(enabled)
        self.apply_btn.setEnabled(False)
        self.bulk_btn.setEnabled(enabled)

    def _get_excluded_folders(self):
        excluded = set()
        root_excluded = False

        def collect(item):
            nonlocal root_excluded
            path = Path(item.data(0, Qt.ItemDataRole.UserRole))
            state = item.checkState(0)
            if path == self.music_dir:
                root_excluded = (state == Qt.CheckState.Unchecked)
                return
            if state == Qt.CheckState.Unchecked:
                excluded.add(path)
            elif state == Qt.CheckState.PartiallyChecked:
                for i in range(item.childCount()):
                    collect(item.child(i))

        root_widget = self.folder_tree.invisibleRootItem()
        for i in range(root_widget.childCount()):
            collect(root_widget.child(i))
        return excluded, root_excluded

    def _set_all_folders(self, checked: bool):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self._updating_checks = True
        try:
            def rec(item):
                item.setCheckState(0, state)
                for i in range(item.childCount()):
                    rec(item.child(i))
            root = self.folder_tree.invisibleRootItem()
            for i in range(root.childCount()):
                rec(root.child(i))
        finally:
            self._updating_checks = False

    # ── Directory / mount ─────────────────────────────────────────────────────

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "Select Music Folder")
        if path:
            self._set_music_dir(Path(path))

    def _busy(self) -> bool:
        """True if any background worker is running (block reset while busy)."""
        for w in (self._scan_worker, self._apply_worker, self._undo_worker,
                  self._organize_worker, self._org_undo_worker,
                  self._dup_worker, self._dup_move_worker, self._dup_undo_worker):
            if w and w.isRunning():
                return True
        return False

    def _clear_source(self):
        if self._busy():
            QMessageBox.warning(self, "Busy",
                                "Wait for the current operation to finish first.")
            return
        if self.tracks and any(track_has_edits(t) for t in self.tracks):
            reply = QMessageBox.question(
                self, "Discard unsaved edits?",
                "You have staged tag/art edits that haven't been applied.\n"
                "Clearing the folder will discard them. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
        self.music_dir = None
        self.tracks = []
        self._undo_records = []
        self._org_undo_records = []
        self._dup_groups = []
        self._dup_undo_records = []
        self.path_edit.clear()
        self.folder_tree.clear()
        self._clear_table()
        self.undo_btn.setEnabled(False)
        self.org_undo_btn.setEnabled(False)
        self.dup_undo_btn.setEnabled(False)
        self.org_target_edit.clear()
        self.dup_quarantine.clear()
        self._rebuild_organize_preview()
        self._populate_dup_table()
        self._set_controls_enabled(False)
        self.clear_btn.setEnabled(False)
        self.statusBar().showMessage("Cleared — select a folder to get started.")

    def _mount_nas(self):
        dlg = MountDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.mounted_path:
            self._mounted_path = Path(dlg.mounted_path)
            self._set_music_dir(self._mounted_path)
            self.mount_btn.setVisible(False)
            self.unmount_btn.setVisible(True)
            self.statusBar().showMessage(f"Mounted NAS at {dlg.mounted_path}")

    def _unmount_nas(self):
        if not self._mounted_path:
            return
        mp = self._mounted_path
        if self._scan_worker and self._scan_worker.isRunning():
            QMessageBox.warning(self, "Busy", "Stop the scan before unmounting.")
            return
        if self._apply_worker and self._apply_worker.isRunning():
            QMessageBox.warning(self, "Busy", "Wait for the apply operation to finish before unmounting.")
            return
        reply = QMessageBox.question(
            self, "Unmount NAS",
            f"Unmount the NAS share at:\n\n  {mp}\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        sudo_pw, ok = QInputDialog.getText(
            self, "Sudo Password", f"Sudo password (to run umount on {mp}):",
            QLineEdit.EchoMode.Password,
        )
        if not ok:
            return
        result = subprocess.run(['sudo', '-S', 'umount', str(mp)],
                                input=sudo_pw + '\n', capture_output=True, text=True)
        if result.returncode != 0:
            err = result.stderr.strip() or "umount failed"
            QMessageBox.critical(self, "Unmount failed", f"Could not unmount {mp}:\n\n{err}")
            return
        self._mounted_path = None
        self.music_dir = None
        self.tracks = []
        self.path_edit.clear()
        self.folder_tree.clear()
        self._clear_table()
        self._set_controls_enabled(False)
        self.undo_btn.setEnabled(False)
        self.unmount_btn.setVisible(False)
        self.mount_btn.setVisible(True)
        self.statusBar().showMessage(f"Unmounted {mp}")

    def _set_music_dir(self, path: Path):
        self.music_dir = path
        self.path_edit.setText(str(path))
        self.tracks = []
        self._undo_records = []
        self.undo_btn.setEnabled(False)
        self._org_undo_records = []
        self.org_undo_btn.setEnabled(False)
        self.org_target_edit.setText(str(path))
        self._dup_groups = []
        self._dup_undo_records = []
        self.dup_undo_btn.setEnabled(False)
        self.dup_quarantine.setText(str(path / "MusicOrganizer-Duplicates"))
        if hasattr(self, "dup_table"):
            self._populate_dup_table()
        self._populate_folder_tree()
        self._clear_table()
        self._rebuild_organize_preview()
        self._set_controls_enabled(True)
        self.clear_btn.setEnabled(True)
        self.statusBar().showMessage("Ready — click Scan Music to begin.")

    # ── Folder tree ───────────────────────────────────────────────────────────

    def _populate_folder_tree(self):
        self.folder_tree.clear()
        if not self.music_dir:
            return
        self._updating_checks = True
        try:
            root_sentinel = QTreeWidgetItem(["(root folder)"])
            root_sentinel.setData(0, Qt.ItemDataRole.UserRole, str(self.music_dir))
            root_sentinel.setCheckState(0, Qt.CheckState.Checked)
            root_sentinel.setToolTip(0, f"Files directly inside {self.music_dir}")
            font = root_sentinel.font(0); font.setItalic(True)
            root_sentinel.setFont(0, font)
            self.folder_tree.addTopLevelItem(root_sentinel)
            self._populate_subtree(self.folder_tree.invisibleRootItem(), self.music_dir)
        except PermissionError as e:
            self.statusBar().showMessage(f"Permission error listing folders: {e}")
        finally:
            self._updating_checks = False
        self.folder_tree.expandToDepth(0)

    def _populate_subtree(self, parent, directory: Path, depth: int = 0):
        if depth > 8:
            return
        try:
            subdirs = sorted(e for e in directory.iterdir()
                             if e.is_dir() and not e.name.startswith('.'))
        except PermissionError:
            return
        for subdir in subdirs:
            item = QTreeWidgetItem([subdir.name])
            item.setData(0, Qt.ItemDataRole.UserRole, str(subdir))
            item.setToolTip(0, str(subdir))
            item.setFlags(item.flags()
                          | Qt.ItemFlag.ItemIsUserCheckable
                          | Qt.ItemFlag.ItemIsAutoTristate)
            item.setCheckState(0, Qt.CheckState.Checked)
            if isinstance(parent, QTreeWidget):
                parent.addTopLevelItem(item)
            else:
                parent.addChild(item)
            self._populate_subtree(item, subdir, depth + 1)

    def _on_folder_check_changed(self, item, column):
        if self._updating_checks:
            return
        self._updating_checks = True
        try:
            state = item.checkState(0)
            if state != Qt.CheckState.PartiallyChecked:
                def propagate(node):
                    for i in range(node.childCount()):
                        child = node.child(i)
                        child.setCheckState(0, state)
                        propagate(child)
                propagate(item)
        finally:
            self._updating_checks = False

    # ── Scan ──────────────────────────────────────────────────────────────────

    def _start_scan(self):
        if not self.music_dir:
            return
        self.scan_btn.setEnabled(False)
        self.apply_btn.setEnabled(False)
        self.progress.setVisible(True); self.progress.setValue(0)
        self.statusBar().showMessage("Scanning music…")
        self._clear_table()
        self._undo_records = []
        self.undo_btn.setEnabled(False)
        self.scan_btn.setVisible(False)
        self.stop_scan_btn.setVisible(True)

        excluded, self._root_excluded = self._get_excluded_folders()
        self._scan_worker = ScanWorker(self.music_dir, excluded)
        self._scan_worker.progress.connect(self._on_scan_progress)
        self._scan_worker.finished.connect(self._on_scan_finished)
        self._scan_worker.error.connect(self._on_scan_error)
        self._scan_worker.start()

    def _on_scan_progress(self, done, total):
        self.progress.setMaximum(total)
        self.progress.setValue(done)

    def _stop_scan(self):
        if self._scan_worker:
            self._scan_worker.stop()
        self.stop_scan_btn.setEnabled(False)
        self.statusBar().showMessage("Stopping scan…")

    def _on_scan_finished(self, tracks, cancelled):
        if self._root_excluded:
            tracks = [t for t in tracks if t['path'].parent != self.music_dir]
        self.tracks = tracks
        self.progress.setVisible(False)
        self.stop_scan_btn.setVisible(False)
        self.stop_scan_btn.setEnabled(True)
        self.scan_btn.setVisible(True)
        self.scan_btn.setEnabled(True)
        if cancelled:
            self.statusBar().showMessage(f"Scan cancelled — {len(tracks)} tracks found so far.")
        else:
            self.statusBar().showMessage(f"Found {len(tracks)} tracks. Edit tags below.")
        self._populate_table()
        self._rebuild_organize_preview()
        self._dup_groups = []
        if hasattr(self, "dup_table"):
            self._populate_dup_table()

    def _on_scan_error(self, msg):
        self.progress.setVisible(False)
        self.stop_scan_btn.setVisible(False)
        self.stop_scan_btn.setEnabled(True)
        self.scan_btn.setVisible(True)
        self.scan_btn.setEnabled(True)
        QMessageBox.critical(self, "Scan Error", msg)

    # ── Table ─────────────────────────────────────────────────────────────────

    def _clear_table(self):
        self._table_updating = True
        try:
            self.table.setRowCount(0)
        finally:
            self._table_updating = False
        self.stats_label.setText("")
        self.apply_btn.setEnabled(False)
        self._reset_art_panel()

    def _populate_table(self):
        self._table_updating = True
        try:
            self.table.setRowCount(0)
            for row, track in enumerate(self.tracks):
                self.table.insertRow(row)
                for col, (key, _) in enumerate(COLUMNS):
                    if key == "__file__":
                        try:
                            disp = str(track['path'].relative_to(self.music_dir))
                        except ValueError:
                            disp = track['path'].name
                        item = QTableWidgetItem(disp)
                        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                        item.setToolTip(str(track['path']))
                    else:
                        item = QTableWidgetItem(track['edited'].get(key, ""))
                        item.setFlags(Qt.ItemFlag.ItemIsEnabled
                                      | Qt.ItemFlag.ItemIsSelectable
                                      | Qt.ItemFlag.ItemIsEditable)
                    self.table.setItem(row, col, item)
                self._recolor_row(row)
        finally:
            self._table_updating = False
        self._update_stats()
        self._update_art_panel()

    def _recolor_row(self, row: int):
        track = self.tracks[row]
        for col, (key, _) in enumerate(COLUMNS):
            if key == "__file__":
                continue
            item = self.table.item(row, col)
            if item is None:
                continue
            value = track['edited'].get(key, "")
            changed = value != track['original'].get(key, "")
            invalid = not validate_value(key, value)
            if invalid:
                item.setBackground(QBrush(self.INVALID_BG))
                item.setForeground(QBrush(self.INVALID_FG))
                item.setToolTip(f"Invalid value for {key}")
            elif changed:
                item.setBackground(QBrush(self.CHANGED_BG))
                item.setForeground(QBrush(self.CHANGED_FG))
                item.setToolTip(f"Changed from: {track['original'].get(key, '')!r}")
            else:
                item.setData(Qt.ItemDataRole.BackgroundRole, None)
                item.setData(Qt.ItemDataRole.ForegroundRole, None)
                item.setToolTip("")

    def _on_table_item_changed(self, item):
        if self._table_updating:
            return
        row, col = item.row(), item.column()
        key = COLUMNS[col][0]
        if key == "__file__" or row >= len(self.tracks):
            return
        self.tracks[row]['edited'][key] = item.text()
        self._table_updating = True
        try:
            self._recolor_row(row)
        finally:
            self._table_updating = False
        self._update_stats()

    def _update_stats(self):
        changed_tracks = sum(1 for t in self.tracks if track_has_edits(t))
        invalid = sum(1 for t in self.tracks for f in FIELDS
                      if not validate_value(f, t['edited'].get(f, "")))
        stat = f"{len(self.tracks)} tracks  |  {changed_tracks} with edits"
        if invalid:
            stat += f"  |  ⚠ {invalid} invalid value(s)"
        self.stats_label.setText(stat)
        self.apply_btn.setEnabled(changed_tracks > 0 and invalid == 0)
        self.apply_btn.setToolTip(
            "Fix invalid values (red cells) before applying" if invalid else "")

    def _apply_bulk(self):
        rows = sorted({idx.row() for idx in self.table.selectionModel().selectedRows()})
        if not rows:
            self.statusBar().showMessage("Select one or more rows first.")
            return
        key = self.bulk_field.currentData()
        value = self.bulk_value.text()
        col = next(i for i, (k, _) in enumerate(COLUMNS) if k == key)
        self._table_updating = True
        try:
            for row in rows:
                self.tracks[row]['edited'][key] = value
                self.table.item(row, col).setText(value)
                self._recolor_row(row)
        finally:
            self._table_updating = False
        self._update_stats()
        self.statusBar().showMessage(
            f"Set {self.bulk_field.currentText()} on {len(rows)} track(s).")

    def _table_context_menu(self, pos):
        row = self.table.rowAt(pos.y())
        if row < 0 or row >= len(self.tracks):
            return
        track = self.tracks[row]
        menu = QMenu(self)
        reset_act = menu.addAction("Reset row to original (tags + art)")
        reset_act.setEnabled(track_has_edits(track))
        action = menu.exec(self.table.viewport().mapToGlobal(pos))
        if action == reset_act:
            track['edited'] = dict(track['original'])
            track['art_action'] = None
            self._table_updating = True
            try:
                for col, (key, _) in enumerate(COLUMNS):
                    if key == "__file__":
                        continue
                    self.table.item(row, col).setText(track['edited'].get(key, ""))
                self._recolor_row(row)
            finally:
                self._table_updating = False
            self._update_stats()
            self._update_art_panel()

    # ── Album art panel ───────────────────────────────────────────────────────

    def _reset_art_panel(self):
        self._art_row = -1
        self.art_view.setText("Select a track")
        self.art_view.setPixmap(QPixmap())
        self.art_info.setText("")
        self.art_replace_btn.setEnabled(False)
        self.art_remove_btn.setEnabled(False)

    def _current_art_row(self) -> int:
        rows = self.table.selectionModel().selectedRows()
        if rows:
            return rows[0].row()
        r = self.table.currentRow()
        return r if 0 <= r < len(self.tracks) else -1

    def _ensure_art_loaded(self, track):
        if not track.get('art_loaded'):
            track['art_original'] = read_art(track['path'])
            track['art_loaded'] = True

    def _current_art_bytes(self, track):
        """(data, mime) currently in effect for this track, or None."""
        action = track.get('art_action')
        if action is not None:
            if action[0] == 'remove':
                return None
            return (action[1], action[2])
        return track.get('art_original')

    def _update_art_panel(self):
        row = self._current_art_row()
        self._art_row = row
        if row < 0 or row >= len(self.tracks):
            self._reset_art_panel()
            return
        track = self.tracks[row]
        ext = track['path'].suffix.lower()
        if ext not in ART_EXTENSIONS:
            self.art_view.setPixmap(QPixmap())
            self.art_view.setText("Art editing not\nsupported for this format")
            self.art_info.setText(track['path'].name)
            self.art_replace_btn.setEnabled(False)
            self.art_remove_btn.setEnabled(False)
            return

        self._ensure_art_loaded(track)
        art = self._current_art_bytes(track)
        pending = track.get('art_action') is not None
        if art is None:
            self.art_view.setPixmap(QPixmap())
            self.art_view.setText("No cover art" + ("\n(removal pending)" if pending else ""))
            self.art_remove_btn.setEnabled(False)
        else:
            data, mime = art
            pix = QPixmap()
            pix.loadFromData(data)
            if pix.isNull():
                self.art_view.setPixmap(QPixmap())
                self.art_view.setText("Unreadable image data")
            else:
                self.art_view.setPixmap(pix.scaled(
                    self.art_view.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
                label = f"{mime.split('/')[-1].upper()} · {len(data) // 1024} KB"
                if pending:
                    label += " · edit pending"
                self.art_info.setText(label)
            self.art_remove_btn.setEnabled(True)
        if art is None:
            self.art_info.setText("edit pending" if pending else "")
        self.art_replace_btn.setEnabled(True)

    def _replace_art(self):
        row = self._art_row
        if row < 0 or row >= len(self.tracks):
            return
        track = self.tracks[row]
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose cover image", "",
            "Images (*.jpg *.jpeg *.png)")
        if not path:
            return
        try:
            data = Path(path).read_bytes()
        except Exception as e:
            QMessageBox.warning(self, "Could not read image", str(e))
            return
        self._ensure_art_loaded(track)
        track['art_action'] = ('replace', data, detect_mime(data))
        self._update_art_panel()
        self._update_stats()
        self.statusBar().showMessage(f"Cover art staged for {track['path'].name}.")

    def _remove_art(self):
        row = self._art_row
        if row < 0 or row >= len(self.tracks):
            return
        track = self.tracks[row]
        self._ensure_art_loaded(track)
        track['art_action'] = ('remove',)
        self._update_art_panel()
        self._update_stats()
        self.statusBar().showMessage(f"Cover art removal staged for {track['path'].name}.")

    # ── Apply ─────────────────────────────────────────────────────────────────

    def _confirm_and_apply(self):
        jobs = []
        for idx, track in enumerate(self.tracks):
            if track_has_edits(track):
                jobs.append((idx, track['path'], diff_track(track), track.get('art_action')))
        if not jobs:
            return

        backup = self.backup_chk.isChecked()
        backup_root = None
        if backup:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_root = self.music_dir / "MusicOrganizer-Backups" / stamp

        msg = (f"Write changes to {len(jobs)} file(s)?\n\n"
               "This modifies the original files in place.\n")
        if backup:
            msg += f"\nOriginals will be backed up to:\n  {backup_root}\n"
        else:
            msg += "\n⚠ Backups are OFF — this cannot be undone after you close the app.\n"
        msg += "\nContinue?"
        reply = QMessageBox.question(
            self, "Confirm Changes", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Snapshot for undo (before we overwrite originals in the model).
        self._undo_records = []
        for idx, path, _, art_action in jobs:
            track = self.tracks[idx]
            art_restore = None
            if art_action is not None:
                prev = track.get('art_original')
                art_restore = ('replace', prev[0], prev[1]) if prev else ('remove',)
            self._undo_records.append({
                'idx': idx, 'path': path,
                'prev_tags': dict(track['original']),
                'art_restore': art_restore,
            })

        self._apply_jobs = jobs
        self._last_backup_root = backup_root
        self.apply_btn.setEnabled(False)
        self.undo_btn.setEnabled(False)
        self.scan_btn.setEnabled(False)
        self.bulk_btn.setEnabled(False)
        self.progress.setVisible(True); self.progress.setValue(0)
        self.statusBar().showMessage("Writing…")

        self._apply_worker = ApplyWorker(jobs, music_dir=self.music_dir,
                                         backup_root=backup_root)
        self._apply_worker.progress.connect(self._on_apply_progress)
        self._apply_worker.finished.connect(self._on_apply_finished)
        self._apply_worker.start()

    def _on_apply_progress(self, done, total):
        self.progress.setMaximum(total)
        self.progress.setValue(done)

    def _on_apply_finished(self, written, errors):
        self.progress.setVisible(False)
        self.scan_btn.setEnabled(True)
        self.bulk_btn.setEnabled(True)

        failed = {p for p, _ in errors}
        succeeded_idx = set()
        for idx, path, _, art_action in self._apply_jobs:
            if str(path) in failed:
                continue
            succeeded_idx.add(idx)
            track = self.tracks[idx]
            track['original'] = dict(track['edited'])
            if art_action is not None:
                if art_action[0] == 'remove':
                    track['art_original'] = None
                else:
                    track['art_original'] = (art_action[1], art_action[2])
                track['art_loaded'] = True
                track['art_action'] = None

        # Only keep undo records for files that actually changed.
        self._undo_records = [r for r in self._undo_records if r['idx'] in succeeded_idx]
        self.undo_btn.setEnabled(bool(self._undo_records))

        self._populate_table()

        extra = ""
        if self._last_backup_root and written:
            extra = f"\n\nBackups saved to:\n  {self._last_backup_root}"
        if errors:
            err_text = "\n".join(f"  {p}: {m}" for p, m in errors[:20])
            if len(errors) > 20:
                err_text += f"\n  … and {len(errors) - 20} more"
            QMessageBox.warning(self, "Done with errors",
                                f"Wrote {written} file(s).\n\n{len(errors)} error(s):\n{err_text}{extra}")
        else:
            QMessageBox.information(self, "Done",
                                    f"Successfully updated {written} file(s).{extra}")
        self.statusBar().showMessage(f"Done — {written} file(s) updated.")

    # ── Undo ──────────────────────────────────────────────────────────────────

    def _undo_last_apply(self):
        if not self._undo_records:
            return
        reply = QMessageBox.question(
            self, "Undo Last Apply",
            f"Restore the previous tags and cover art on "
            f"{len(self._undo_records)} file(s)?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return

        jobs = [(r['idx'], r['path'], r['prev_tags'], r['art_restore'])
                for r in self._undo_records]
        self.undo_btn.setEnabled(False)
        self.apply_btn.setEnabled(False)
        self.scan_btn.setEnabled(False)
        self.bulk_btn.setEnabled(False)
        self.progress.setVisible(True); self.progress.setValue(0)
        self.statusBar().showMessage("Undoing…")

        self._undo_jobs = jobs
        self._undo_worker = ApplyWorker(jobs, music_dir=self.music_dir, backup_root=None)
        self._undo_worker.progress.connect(self._on_apply_progress)
        self._undo_worker.finished.connect(self._on_undo_finished)
        self._undo_worker.start()

    def _on_undo_finished(self, written, errors):
        self.progress.setVisible(False)
        self.scan_btn.setEnabled(True)
        self.bulk_btn.setEnabled(True)

        failed = {p for p, _ in errors}
        for idx, path, _, _ in self._undo_jobs:
            if str(path) in failed:
                continue
            track = self.tracks[idx]
            fresh = read_tags(track['path'])
            if fresh is not None:
                track['original'] = dict(fresh)
                track['edited'] = dict(fresh)
            track['art_action'] = None
            track['art_original'] = read_art(track['path'])
            track['art_loaded'] = True

        self._undo_records = []
        self._populate_table()

        if errors:
            err_text = "\n".join(f"  {p}: {m}" for p, m in errors[:20])
            QMessageBox.warning(self, "Undo done with errors",
                                f"Restored {written} file(s).\n\n{len(errors)} error(s):\n{err_text}")
        else:
            QMessageBox.information(self, "Undo complete",
                                    f"Restored previous tags/art on {written} file(s).")
        self.statusBar().showMessage(f"Undo complete — {written} file(s) restored.")


# ── Flexoki theme ─────────────────────────────────────────────────────────────

FLEXOKI_STYLESHEET = """
/* ── Flexoki (dark) ───────────────────────────────────────────────────────── */

QWidget {
    background-color: #100f0f;   /* base black */
    color: #cecdc3;              /* base 200 */
    font-size: 13px;
}
QMainWindow, QDialog { background-color: #100f0f; }

/* Tabs */
QTabWidget::pane {
    border: 1px solid #403e3c; border-radius: 4px; top: -1px;
}
QTabBar::tab {
    background-color: #1c1b1a; color: #878580;
    border: 1px solid #403e3c; border-bottom: none;
    border-top-left-radius: 4px; border-top-right-radius: 4px;
    padding: 6px 18px; margin-right: 2px;
}
QTabBar::tab:selected { background-color: #100f0f; color: #4385be; font-weight: bold; }
QTabBar::tab:hover:!selected { color: #cecdc3; }

QGroupBox {
    border: 1px solid #403e3c;
    border-radius: 6px;
    margin-top: 10px;
    padding-top: 6px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px; padding: 0 4px;
    color: #4385be;              /* blue 400 */
}

QPushButton {
    background-color: #1c1b1a;
    color: #cecdc3;
    border: 1px solid #403e3c;
    border-radius: 4px;
    padding: 4px 14px;
    min-height: 22px;
}
QPushButton:hover { background-color: #282726; border-color: #4385be; }
QPushButton:pressed { background-color: #343331; }
QPushButton:disabled { background-color: #100f0f; color: #575653; border-color: #282726; }

QPushButton#applyBtn {
    background-color: #4385be; color: #100f0f; border: none; font-weight: bold;
}
QPushButton#applyBtn:hover { background-color: #66a0ce; }
QPushButton#applyBtn:disabled { background-color: #1c1b1a; color: #575653; }

QPushButton#stopBtn {
    background-color: #d14d41; color: #100f0f; border: none; font-weight: bold;
}
QPushButton#stopBtn:hover { background-color: #e0685b; }

QLineEdit {
    background-color: #1c1b1a; color: #cecdc3;
    border: 1px solid #403e3c; border-radius: 4px; padding: 4px 8px;
    selection-background-color: #4385be; selection-color: #100f0f;
}
QLineEdit:focus { border-color: #4385be; }
QLineEdit:read-only { color: #878580; }

QComboBox {
    background-color: #1c1b1a; color: #cecdc3;
    border: 1px solid #403e3c; border-radius: 4px; padding: 4px 8px;
}
QComboBox:hover { border-color: #4385be; }
QComboBox QAbstractItemView {
    background-color: #1c1b1a; color: #cecdc3;
    selection-background-color: #343331; border: 1px solid #403e3c; outline: none;
}

QCheckBox { spacing: 6px; }
QCheckBox::indicator {
    width: 15px; height: 15px;
    border: 1px solid #403e3c; border-radius: 3px; background-color: #1c1b1a;
}
QCheckBox::indicator:hover { border-color: #4385be; }
QCheckBox::indicator:checked { background-color: #4385be; border-color: #4385be; }

QTreeWidget {
    background-color: #1c1b1a; alternate-background-color: #100f0f;
    color: #cecdc3; border: 1px solid #403e3c; border-radius: 4px; outline: none;
}
QTreeWidget::item { padding: 3px 0; }
QTreeWidget::item:hover { background-color: #282726; }
QTreeWidget::item:selected { background-color: #343331; color: #cecdc3; }

/* Folder include/exclude checkboxes — bordered for contrast, blue when on */
QTreeWidget::indicator {
    width: 15px; height: 15px;
    border: 1px solid #6f6e69; border-radius: 3px; background-color: #100f0f;
}
QTreeWidget::indicator:hover { border-color: #4385be; }
QTreeWidget::indicator:checked { background-color: #4385be; border-color: #4385be; }
QTreeWidget::indicator:indeterminate {
    background-color: #205ea6; border-color: #4385be;
}
/* Branch (expand/collapse) arrow images are injected at runtime in main() */

QTableWidget {
    background-color: #1c1b1a; alternate-background-color: #100f0f;
    color: #cecdc3; border: 1px solid #403e3c; gridline-color: #282726; outline: none;
}
QTableWidget::item { padding: 3px 6px; }
QTableWidget::item:selected { background-color: #343331; color: #cecdc3; }

QHeaderView { background-color: #0d0c0c; }
QHeaderView::section {
    background-color: #0d0c0c; color: #4385be; border: none;
    border-right: 1px solid #282726; border-bottom: 1px solid #282726;
    padding: 5px 8px; font-weight: bold;
}
QHeaderView::section:last { border-right: none; }

QLabel#statsLabel { color: #878580; }
QLabel#artView {
    background-color: #1c1b1a; border: 1px solid #403e3c; border-radius: 4px;
    color: #878580;
}

QProgressBar {
    background-color: #1c1b1a; border: 1px solid #403e3c; border-radius: 4px;
    text-align: center; color: #cecdc3; height: 16px;
}
QProgressBar::chunk { background-color: #4385be; border-radius: 3px; }

QScrollBar:vertical { background-color: #1c1b1a; width: 10px; border: none; margin: 0; }
QScrollBar::handle:vertical { background-color: #403e3c; border-radius: 5px; min-height: 24px; margin: 2px; }
QScrollBar::handle:vertical:hover { background-color: #575653; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
QScrollBar:horizontal { background-color: #1c1b1a; height: 10px; border: none; margin: 0; }
QScrollBar::handle:horizontal { background-color: #403e3c; border-radius: 5px; min-width: 24px; margin: 2px; }
QScrollBar::handle:horizontal:hover { background-color: #575653; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: none; }

QSplitter::handle { background-color: #282726; }
QSplitter::handle:horizontal { width: 2px; }
QSplitter::handle:vertical { height: 2px; }

QStatusBar { background-color: #0d0c0c; color: #878580; border-top: 1px solid #282726; }
QLabel { background: transparent; color: #cecdc3; }
QFrame[frameShape="4"], QFrame[frameShape="5"] { color: #403e3c; }

QMessageBox { background-color: #100f0f; }
QMessageBox QLabel { color: #cecdc3; }
QMessageBox QPushButton { min-width: 80px; }
QDialogButtonBox QPushButton { min-width: 80px; }
"""


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Music Organizer")
    app.setStyle("Fusion")

    stylesheet = FLEXOKI_STYLESHEET
    try:
        icon_dir = Path(tempfile.mkdtemp(prefix="musicorganizer-icons-"))
        closed, open_ = generate_tree_icons(icon_dir)
        stylesheet += tree_icon_stylesheet(closed, open_)
    except Exception:
        pass  # arrows are a nicety; fall back to the default look if generation fails
    app.setStyleSheet(stylesheet)

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
