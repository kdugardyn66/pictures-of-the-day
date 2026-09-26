"""Picture store: <root>/<Site>/<YYYY-MM-DD>_<slug>.<ext> plus a small metadata index."""
from __future__ import annotations

import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from .config import PRIVATE_SITE_DIRS, SITES, _read_json, write_json_atomic

FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_.+\.(jpe?g|png)$", re.I)
META_NAME = ".potd-meta.json"


@dataclass(frozen=True)
class StoredPicture:
    site: str
    path: Path
    date: str

    @property
    def rel(self) -> str:
        return f"{self.site}/{self.path.name}"


class Storage:
    def __init__(self, root, site_dirs: dict | None = None):
        self.root = Path(root)
        # Sites stored outside the shared root (your Photos library copies).
        self.site_dirs = {k: Path(v) for k, v in (PRIVATE_SITE_DIRS if site_dirs is None else site_dirs).items()}
        self._lock = threading.RLock()

    # ---------- files ----------
    def site_dir(self, site: str, create: bool = False) -> Path:
        d = self.site_dirs.get(site) or self.root / site
        if create:
            d.mkdir(parents=True, exist_ok=True)
            if site in self.site_dirs:
                os.chmod(d, 0o700)
        return d

    def pictures(self, site: str) -> list[StoredPicture]:
        """Pictures of one site, newest first."""
        d = self.site_dir(site)
        out = []
        try:
            entries = list(os.scandir(d))
        except OSError:
            return []
        for e in entries:
            m = FILE_RE.match(e.name)
            if m and e.is_file():
                try:
                    mt = e.stat().st_mtime
                except OSError:
                    mt = 0
                out.append((m.group(1), mt, e.name, StoredPicture(site, Path(e.path), m.group(1))))
        out.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
        return [t[3] for t in out]

    def resolve(self, rel: str) -> StoredPicture | None:
        site, _, name = rel.partition("/")
        m = FILE_RE.match(name)
        p = self.site_dir(site) / name
        if site in SITES and m and p.is_file():
            return StoredPicture(site, p, m.group(1))
        return None

    def save_bytes(self, site: str, date: str, slug: str, ext: str, data: bytes) -> Path:
        d = self.site_dir(site, create=True)
        with self._lock:
            base = f"{date}_{slug}"
            path = d / f"{base}.{ext}"
            n = 2
            while path.exists():
                path = d / f"{base}-{n}.{ext}"
                n += 1
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".dl-")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.chmod(tmp, 0o600 if site in self.site_dirs else 0o644)
            os.replace(tmp, path)
        return path

    # ---------- per-screen versions (Photos) ----------
    @staticmethod
    def _variant_dir(path: Path) -> Path:
        return Path(path).parent / ".screens"

    def save_variant(self, path: Path, size: tuple, data: bytes) -> Path:
        """Store a version of `path` rendered for one screen size (hidden from rotation)."""
        d = self._variant_dir(path)
        d.mkdir(parents=True, exist_ok=True)
        out = d / f"{Path(path).stem}@{int(size[0])}x{int(size[1])}.jpg"
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".dl-")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o600 if Path(path).parent in self.site_dirs.values() else 0o644)
        os.replace(tmp, out)
        return out

    def variants(self, path: Path) -> dict:
        """{(w, h): file} of the per-screen versions of a picture."""
        path, out = Path(path), {}
        pre = path.stem + "@"
        try:
            entries = list(os.scandir(self._variant_dir(path)))
        except OSError:
            return out
        for e in entries:
            if e.name.startswith(pre) and e.name.endswith(".jpg"):
                try:
                    w, h = e.name[len(pre):-4].split("x")
                    out[(int(w), int(h))] = Path(e.path)
                except ValueError:
                    pass
        return out

    def _remove_variants(self, path: Path) -> None:
        for f in self.variants(path).values():
            try:
                f.unlink()
            except OSError:
                pass

    def prune(self, site: str, keep_days: int) -> list[Path]:
        """Keep the pictures of the newest `keep_days` distinct days; delete the rest."""
        pics = self.pictures(site)
        dates = sorted({p.date for p in pics}, reverse=True)
        keep = set(dates[: max(1, keep_days)])
        removed = []
        for p in pics:
            if p.date not in keep:
                try:
                    p.path.unlink()
                    removed.append(p.path)
                except OSError:
                    pass
                self._remove_variants(p.path)
        if removed:
            with self._lock:
                meta = self._meta()
                for p in removed:
                    meta.pop(f"{site}/{p.name}", None)
                self._save_meta(meta)
        return removed

    # ---------- metadata ----------
    def _meta(self) -> dict:
        return _read_json(self.root / META_NAME)

    def _save_meta(self, meta: dict) -> None:
        try:
            write_json_atomic(self.root / META_NAME, meta)
        except OSError:
            pass

    def meta_get(self, rel: str) -> dict:
        with self._lock:
            v = self._meta().get(rel)
            return v if isinstance(v, dict) else {}

    def meta_set(self, rel: str, info: dict) -> None:
        with self._lock:
            meta = self._meta()
            meta[rel] = info
            self._save_meta(meta)

    def find_by_source(self, site: str, source_url: str) -> StoredPicture | None:
        with self._lock:
            meta = self._meta()
        for rel, info in meta.items():
            if rel.startswith(site + "/") and isinstance(info, dict) and info.get("source_url") == source_url:
                sp = self.resolve(rel)
                if sp:
                    return sp
        return None

    def daily_entry(self, site: str) -> dict | None:
        """{"date", "rel", "done"} of the last daily fetch of a site, or None."""
        with self._lock:
            m = (self._meta().get("_daily") or {}).get(site)
        return m if isinstance(m, dict) else None

    def set_daily_marker(self, site: str, date: str, rel: str, done: bool = True) -> None:
        with self._lock:
            meta = self._meta()
            daily = meta.get("_daily") or {}
            daily[site] = {"date": date, "rel": rel, "done": done}
            meta["_daily"] = daily
            self._save_meta(meta)

    def recent_photos(self) -> list[str]:
        """Photos library items used lately (so the same photo doesn't come back soon)."""
        with self._lock:
            return list(self._meta().get("_photos_recent") or [])

    def add_recent_photo(self, local_id: str, keep: int = 300) -> None:
        with self._lock:
            meta = self._meta()
            lst = [x for x in (meta.get("_photos_recent") or []) if x != local_id] + [local_id]
            meta["_photos_recent"] = lst[-keep:]
            self._save_meta(meta)

    def place_cache(self) -> dict:
        """Place names already looked up, by rounded coordinates (fewer geocoding calls)."""
        with self._lock:
            return dict(self._meta().get("_places") or {})

    def save_place_cache(self, places: dict, keep: int = 2000) -> None:
        with self._lock:
            meta = self._meta()
            meta["_places"] = dict(list(places.items())[-keep:])
            self._save_meta(meta)

    def is_rejected(self, url: str) -> bool:
        with self._lock:
            return url in (self._meta().get("_rejected") or [])

    def reject(self, url: str) -> None:
        with self._lock:
            meta = self._meta()
            lst = [u for u in (meta.get("_rejected") or []) if u != url]
            lst.append(url)
            meta["_rejected"] = lst[-100:]
            self._save_meta(meta)
