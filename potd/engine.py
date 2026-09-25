"""The app's brain: schedule, downloads, rotation pools and wallpaper assignment.

It has no AppKit dependency. The UI passes in a *backend* object that knows the
current screens/Spaces and how to set a wallpaper, so this module can be unit-tested
anywhere.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from pathlib import Path
from typing import Protocol

from .config import API_KEY_FIELDS, SITES, Settings, State
from .sources import FetchContext, Picture, SourceError, cached_today, fetch, test_source
from .storage import Storage

log = logging.getLogger("potd")
MAX_SLOTS = 64


class Backend(Protocol):
    def slots(self) -> list[tuple[str, object]]: ...        # [(slot_key, screen), ...]
    def set_wallpaper(self, screen, path: Path) -> bool: ...
    def target_size(self) -> tuple[int, int]: ...


class Engine:
    def __init__(self, settings: Settings, state: State, backend: Backend,
                 settings_path=None, state_path=None):
        self.settings = settings
        self.state = state
        self.backend = backend
        self._settings_path = settings_path
        self._state_path = state_path
        self.storage = Storage(settings.storage_root, {"Photos": settings.photos_dir})
        self.override: Path | None = None       # "Set wallpaper now" picture
        self.override_keys: set[str] = set()    # ...only on the screens/Spaces visible then
        self._applied: dict[str, str] = {}
        self._dl_lock = threading.Lock()

    # ------------------------------------------------------------ persistence
    def save_state(self):
        try:
            self.state.save(self._state_path)
        except OSError as e:
            log.warning("could not save state: %s", e)

    def save_settings(self):
        self.settings.save(self._settings_path)
        if Path(self.settings.storage_root) != self.storage.root:
            self.storage = Storage(self.settings.storage_root, {"Photos": self.settings.photos_dir})

    # ------------------------------------------------------------ schedule
    def next_download_at(self, now: dt.datetime | None = None) -> dt.datetime:
        now = now or dt.datetime.now()
        h, m = self.settings.download_hm
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if self.state.last_download_date == now.date().isoformat():
            target += dt.timedelta(days=1)
        return target

    def download_due(self, now: dt.datetime | None = None, now_ts: float | None = None) -> bool:
        now = now or dt.datetime.now()
        now_ts = time.time() if now_ts is None else now_ts
        if not self.settings.enabled_sites():
            return False
        if self.state.last_download_date == now.date().isoformat():
            return False
        h, m = self.settings.download_hm
        return now >= now.replace(hour=h, minute=m, second=0, microsecond=0)

    def retry_due(self) -> list[str]:
        """Sources that failed temporarily; retried at every refresh."""
        return [s for s in self.state.retry_sites if self.settings.enabled.get(s)]

    def rotation_due(self, now_ts: float | None = None) -> bool:
        now_ts = time.time() if now_ts is None else now_ts
        return now_ts - self.state.last_rotation_ts >= self.settings.refresh_rate

    # ------------------------------------------------------------ downloading (worker thread)
    def _ctx(self) -> FetchContext:
        w, h = self.backend.target_size()
        s = self.settings
        return FetchContext(self.storage, dt.date.today().isoformat(), w, h,
                            s.nasa_api_key, s.unsplash_access_key, s.bing_market,
                            photo_caption=s.photo_caption)

    def run_downloads(self, sites: list[str] | None = None) -> dict:
        """Fetch today's picture from each site. Blocking: call from a worker thread."""
        sites = self.settings.enabled_sites() if sites is None else sites
        results: dict[str, object] = {}
        with self._dl_lock:
            ctx = self._ctx()
            for site in sites:
                try:
                    results[site] = fetch(site, ctx)
                    log.info("%s: %s", site, results[site].path.name)
                except Exception as e:           # one bad site must not stop the others
                    results[site] = e
                    log.warning("%s failed: %s", site, e)
            self.prune()
        return results

    def fetch_one(self, site: str) -> Picture:
        """Manual click on a site. Blocking: call from a worker thread."""
        with self._dl_lock:
            pic = fetch(site, self._ctx())
            self.storage.prune(site, self.settings.keep_days)
            return pic

    def test_sources(self, sites: list[str] | None = None, keys: dict | None = None) -> dict:
        """{site: (ok, message, network_error)}. Blocking: call from a worker thread.
        `keys` = {site: api_key} tests with keys that aren't saved yet (info window)."""
        sites = self.settings.enabled_sites() if sites is None else sites
        ctx = self._ctx()
        for site, key in (keys or {}).items():
            attr = API_KEY_FIELDS.get(site)
            if attr:
                key = (key or "").strip()
                setattr(ctx, attr, key or ("DEMO_KEY" if site == "NASA" else ""))
        out = {}
        for site in sites:
            if not (keys and site in keys) and cached_today(site, ctx):
                out[site] = (True, "today's picture already downloaded", False)   # no network call
                continue
            try:
                out[site] = (True, test_source(site, ctx), False)
            except SourceError as e:
                out[site] = (False, str(e), e.transient)
            except Exception as e:
                out[site] = (False, str(e), False)
            log.info("test %s: %s", site, out[site][1])
        return out

    def save_source(self, site: str, api_key: str | None = None) -> None:
        """Store a tested API key and enable the source (info window "Save")."""
        if api_key is not None:
            self.settings.set_api_key(site, api_key)
        self.settings.enabled[site] = True
        self.save_settings()
        self.apply()

    def apply_test_results(self, results: dict) -> tuple[list[str], bool]:
        """Disable every source whose test failed permanently (e.g. bad key).
        Temporary failures (offline, rate limit, server error) don't disable anything.
        Returns (disabled sites, offline)."""
        failed = [s for s, (ok, _, _) in results.items() if not ok]
        offline = bool(failed) and len(failed) == len(results) and all(results[s][2] for s in failed)
        failed = [s for s in failed if not results[s][2]]
        for s in failed:
            self.settings.enabled[s] = False
        if failed:
            self.save_settings()
            self.apply()
        return failed, offline

    def prune(self):
        for site in SITES:
            self.storage.prune(site, self.settings.keep_days)

    # ------------------------------------------------------------ completion (main thread)
    def finish_downloads(self, results: dict, now_ts: float | None = None, daily: bool = True) -> dict:
        """Handle the results of any download run (at open, daily, retry, "Download All Now").
        - picture          -> part of today's pictures
        - temporary error  -> retried at the next refresh
        - permanent error  -> source disabled (e.g. rejected API key)
        - older picture    -> used for now, checked again at the next refresh
        `daily`: this run counts as today's scheduled download."""
        now_ts = time.time() if now_ts is None else now_ts
        today = dt.date.today().isoformat()
        st = self.state
        st.last_attempt_ts = now_ts
        if daily:
            st.last_download_date = today          # retries are handled per source below
        pics = [results[s] for s in SITES if isinstance(results.get(s), Picture)]
        retry = [s for s in SITES if (isinstance(results.get(s), Exception)
                                      and getattr(results[s], "transient", False))
                 or (isinstance(results.get(s), Picture) and results[s].stale)]
        disabled = [s for s in SITES if isinstance(results.get(s), Exception)
                    and not getattr(results[s], "transient", False)]
        st.retry_sites = [s for s in dict.fromkeys(st.retry_sites + retry)
                          if s not in results or s in retry]
        for s in disabled:
            self.settings.enabled[s] = False
        if disabled:
            self.save_settings()

        new_day = bool(pics) and st.day_pool_date != today
        pool = [] if new_day else list(st.day_pool)
        for p in pics:
            if p.rel not in pool:
                pool.append(p.rel)
        if pics:
            st.day_pool, st.day_pool_date = pool, today
        if new_day:
            # A new day starts with the first picture of the day.
            st.offset = 0
            st.last_rotation_ts = now_ts
            self.override = None
            self.override_keys = set()
        self.save_state()
        if pics or disabled:
            self.apply(force=new_day)
        return {"pictures": pics, "new": [p for p in pics if p.new], "retry": retry,
                "disabled": disabled, "errors": {s: str(e) for s, e in results.items()
                                                 if isinstance(e, Exception)}}

    def finish_fetch(self, pic: Picture):
        today = dt.date.today().isoformat()
        if self.state.day_pool_date == today and pic.rel not in self.state.day_pool:
            self.state.day_pool.append(pic.rel)
            self.save_state()

    # ------------------------------------------------------------ pools
    def _enabled(self, rel: str) -> bool:
        return self.settings.enabled.get(rel.partition("/")[0], False)

    def day_pool(self) -> list[Path]:
        """The pictures of the day (Clone = Yes loops through these)."""
        out = []
        for rel in self.state.day_pool:
            sp = self.storage.resolve(rel)
            if sp and self._enabled(rel):
                out.append(sp.path)
        if out:
            return out
        # Fallback: the newest day we have anything for.
        pics = [p for s in self.settings.enabled_sites() for p in self.storage.pictures(s)]
        if not pics:
            return []
        newest = max(p.date for p in pics)
        return [p.path for s in SITES for p in self.storage.pictures(s)
                if p.date == newest and self._enabled(p.rel)]

    def all_pool(self) -> list[Path]:
        """Today's pictures first, then day-1, day-2, ... (Clone = No loops through these)."""
        seen, out = set(), []
        for p in self.day_pool():
            seen.add(p)
            out.append(p)
        by_date: dict[str, list[Path]] = {}
        for site in self.settings.enabled_sites():
            for sp in self.storage.pictures(site):
                by_date.setdefault(sp.date, []).append(sp.path)
        for d in sorted(by_date, reverse=True):
            for p in by_date[d]:
                if p not in seen:
                    seen.add(p)
                    out.append(p)
        return out

    # ------------------------------------------------------------ assignment
    def slot_index(self, key: str) -> int:
        slots = self.state.slots
        if key not in slots:
            slots.append(key)
            if len(slots) > MAX_SLOTS:
                del slots[0]
            self.save_state()
        return slots.index(key)

    def plan(self) -> list[tuple[str, object, Path]]:
        """Which picture goes on which (display, Space) slot right now."""
        slots = self.backend.slots()
        ov = Path(self.override) if self.override and Path(self.override).exists() else None
        if self.settings.clone_wallpapers:
            pool = self.day_pool()
            pick = (lambda k: pool[self.state.offset % len(pool)]) if pool else None
        else:
            pool = self.all_pool()
            pick = (lambda k: pool[(self.state.offset + self.slot_index(k)) % len(pool)]) if pool else None
        out = []
        for k, scr in slots:
            if ov and k in self.override_keys:
                out.append((k, scr, ov))
            elif pick:
                out.append((k, scr, pick(k)))
        return out

    def apply(self, force: bool = False) -> int:
        changed = 0
        plan = self.plan()
        if not plan:
            log.info("apply: nothing to show (no pictures for the enabled sources)")
        for key, screen, path in plan:
            if force or self._applied.get(key) != str(path):
                ok = self.backend.set_wallpaper(screen, path)
                log.info("apply: clone=%s offset=%d slot=%s -> %s%s",
                         "yes" if self.settings.clone_wallpapers else "no", self.state.offset,
                         key, path.name, "" if ok else "  (FAILED)")
                if ok:
                    self._applied[key] = str(path)
                    changed += 1
        return changed

    def rotate(self, now_ts: float | None = None) -> int:
        self.override = None
        self.override_keys = set()
        if self.settings.clone_wallpapers:
            step = 1
        else:
            # Move every slot on to pictures it hasn't shown; if there are fewer pictures
            # than slots, a plain +1 still guarantees every screen changes.
            n = len(self.all_pool()) or 1
            step = (max(1, len(self.state.slots)) % n) or 1
        self.state.offset += step
        self.state.last_rotation_ts = time.time() if now_ts is None else now_ts
        self.save_state()
        return self.apply()

    def set_now(self, path: Path) -> int:
        self.override = Path(path)
        self.override_keys = {k for k, _ in self.backend.slots()}
        self.state.last_rotation_ts = time.time()
        self.save_state()
        return self.apply(force=True)

    def caption(self, path: Path) -> str:
        path = Path(path)
        m = self.storage.meta_get(f"{path.parent.name}/{path.name}")
        parts = [x for x in (m.get("title"), m.get("credit")) if x]
        return " — ".join(parts) or path.name
