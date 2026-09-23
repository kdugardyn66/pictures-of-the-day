"""Settings and runtime state, persisted as JSON in ~/Library/Application Support/potd."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

__all__ = ["SITES", "API_KEY_FIELDS", "Settings", "State", "SUPPORT_DIR", "DEFAULT_ROOT"]

# SHA-256 of the Unsplash key that v1.0 shipped as a default; it is cleared from old settings.
_OLD_UNSPLASH_KEY_SHA256 = "eaffb0cea97dc13dd6aadb3767edfc1ab059ae6e08a7836ba2588a1ac2986f63"

SITES = ["Bing", "NASA", "National Geographic", "Unsplash", "Wikimedia", "PicSum"]
DEFAULT_ROOT = "/Users/Shared/Pictures/potd"
API_KEY_FIELDS = {"NASA": "nasa_api_key", "Unsplash": "unsplash_access_key"}
SUPPORT_DIR = Path(
    os.environ.get("POTD_SUPPORT_DIR")
    or Path.home() / "Library" / "Application Support" / "potd"
)
TIME_RE = re.compile(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$")


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json_atomic(path: Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_into(obj, raw: dict):
    for f in fields(obj):
        if f.name in raw:
            setattr(obj, f.name, raw[f.name])
    return obj


@dataclass
class Settings:
    download_time: str = "09:00"      # daily download, HH:MM local time
    refresh_rate: int = 3600          # seconds between wallpaper changes (whole hours, 1-24 h)
    clone_wallpapers: bool = True     # same picture everywhere?
    keep_days: int = 30               # days of pictures kept per site
    enabled: dict = field(default_factory=lambda: {s: True for s in SITES})
    storage_root: str = DEFAULT_ROOT
    nasa_api_key: str = "DEMO_KEY"
    unsplash_access_key: str = ""       # entered by the user in the Unsplash info window
    bing_market: str = "en-US"
    start_at_login: bool = False
    settings_version: int = 2         # 1 = potd 1.0 (had a built-in Unsplash key)

    MIN_REFRESH_H, MAX_REFRESH_H = 1, 24   # class constants, not fields

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        raw = _read_json(path or SUPPORT_DIR / "settings.json")
        s = _load_into(cls(), raw)
        # One-time upgrade from 1.0: drop the Unsplash key that used to be built in.
        # A key the user enters later (even the same one) is kept.
        if raw and int(raw.get("settings_version", 1) or 1) < 2:
            key = str(s.unsplash_access_key or "").strip()
            if hashlib.sha256(key.encode()).hexdigest() == _OLD_UNSPLASH_KEY_SHA256:
                s.unsplash_access_key = ""
        s.settings_version = 2
        s.normalize()
        return s

    def save(self, path: Path | None = None) -> None:
        self.normalize()
        write_json_atomic(path or SUPPORT_DIR / "settings.json", asdict(self))

    def normalize(self) -> None:
        m = TIME_RE.match(str(self.download_time))
        self.download_time = f"{int(m.group(1)):02d}:{m.group(2)}" if m else "09:00"
        try:
            hours = round(int(self.refresh_rate) / 3600)
        except (TypeError, ValueError):
            hours = 1
        self.refresh_rate = min(self.MAX_REFRESH_H, max(self.MIN_REFRESH_H, hours)) * 3600
        try:
            self.keep_days = max(1, int(self.keep_days))
        except (TypeError, ValueError):
            self.keep_days = 30
        self.clone_wallpapers = bool(self.clone_wallpapers)
        self.start_at_login = bool(self.start_at_login)
        en = self.enabled if isinstance(self.enabled, dict) else {}
        self.enabled = {s: bool(en.get(s, True)) for s in SITES}
        self.storage_root = str(self.storage_root or DEFAULT_ROOT)
        self.nasa_api_key = str(self.nasa_api_key or "").strip() or "DEMO_KEY"
        self.unsplash_access_key = str(self.unsplash_access_key or "").strip()

    @property
    def refresh_hours(self) -> int:
        return self.refresh_rate // 3600

    @refresh_hours.setter
    def refresh_hours(self, hours) -> None:
        try:
            hours = int(hours)
        except (TypeError, ValueError):
            hours = 1
        self.refresh_rate = min(self.MAX_REFRESH_H, max(self.MIN_REFRESH_H, hours)) * 3600

    @property
    def download_hm(self) -> tuple[int, int]:
        h, m = self.download_time.split(":")
        return int(h), int(m)

    def api_key(self, site: str) -> str | None:
        """The API key of a site, or None if the site doesn't use one."""
        attr = API_KEY_FIELDS.get(site)
        return getattr(self, attr) if attr else None

    def set_api_key(self, site: str, value: str) -> None:
        attr = API_KEY_FIELDS.get(site)
        if attr:
            setattr(self, attr, (value or "").strip())
            self.normalize()

    def enabled_sites(self) -> list[str]:
        return [s for s in SITES if self.enabled.get(s)]


@dataclass
class State:
    last_download_date: str | None = None   # YYYY-MM-DD of last successful daily run
    last_attempt_ts: float = 0.0             # last daily attempt (for retry back-off)
    offset: int = 0                          # rotation position
    last_rotation_ts: float = 0.0
    day_pool_date: str | None = None         # the "pictures of the day"
    day_pool: list = field(default_factory=list)   # ["Site/file.jpg", ...]
    slots: list = field(default_factory=list)      # known "<display>|<space>" keys, in first-seen order
    retry_sites: list = field(default_factory=list)  # failed temporarily -> retried every refresh

    @classmethod
    def load(cls, path: Path | None = None) -> "State":
        st = _load_into(cls(), _read_json(path or SUPPORT_DIR / "state.json"))
        st.day_pool = [str(x) for x in (st.day_pool or [])]
        st.slots = [str(x) for x in (st.slots or [])]
        st.retry_sites = [str(x) for x in (st.retry_sites or []) if x in SITES]
        return st

    def save(self, path: Path | None = None) -> None:
        write_json_atomic(path or SUPPORT_DIR / "state.json", asdict(self))
