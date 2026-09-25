"""Core-logic tests (no macOS needed):  python3 -m pytest tests -q"""
import datetime as dt
import struct
import zlib
from pathlib import Path

import pytest

from potd import engine as engine_mod
from potd import sources
from potd.config import Settings, State
from potd.engine import Engine
from potd.sources import Picture, image_info, is_wallpaper_shaped


def png(w, h):
    raw = b"".join(b"\x00" + b"\x00" * (w * 3) for _ in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class FakeBackend:
    def __init__(self, keys):
        self.keys = keys
        self.set = {}

    def slots(self):
        return [(k, k) for k in self.keys]

    def set_wallpaper(self, screen, path):
        self.set[screen] = Path(path).name
        return True

    def target_size(self):
        return 2560, 1440


@pytest.fixture
def env(tmp_path):
    s = Settings(storage_root=str(tmp_path / "potd"), photos_dir=str(tmp_path / "private" / "Photos"))
    st = State()
    be = FakeBackend(["A|1", "B|1"])
    e = Engine(s, st, be, settings_path=tmp_path / "s.json", state_path=tmp_path / "st.json")
    return e, be, tmp_path


def add(e, site, date, name):
    p = e.storage.save_bytes(site, date, name, "png", png(4, 2))
    return p


def test_image_info_and_shape():
    assert image_info(png(40, 20)) == (40, 20, "png")
    jpg = b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9 + \
          b"\xff\xc0" + struct.pack(">HBHH", 17, 8, 1440, 2560) + b"\x00" * 12
    assert image_info(jpg) == (2560, 1440, "jpg")
    assert image_info(b"<html>") is None
    assert is_wallpaper_shaped(2560, 1440) and not is_wallpaper_shaped(1440, 2560)
    assert not is_wallpaper_shaped(1200, 600)


def test_settings_normalize(tmp_path):
    s = Settings(download_time="7:5", refresh_rate="10", keep_days=0, enabled={"Bing": False})
    s.save(tmp_path / "x.json")
    s2 = Settings.load(tmp_path / "x.json")
    assert s2.download_time == "09:00"      # invalid "7:5" -> default
    assert s2.refresh_rate == 3600 and s2.keep_days == 1     # 10 s -> minimum 1 hour
    assert s2.enabled["Bing"] is False and s2.enabled["NASA"] is True
    s3 = Settings(download_time="7:05"); s3.normalize(); assert s3.download_hm == (7, 5)


def test_prune_keeps_n_days(env):
    e, _, _ = env
    for d in ["2026-09-20", "2026-09-21", "2026-09-22", "2026-09-22", "2026-09-23"]:
        add(e, "PicSum", d, "x")
    removed = e.storage.prune("PicSum", 2)
    left = sorted(p.date for p in e.storage.pictures("PicSum"))
    assert left == ["2026-09-22", "2026-09-22", "2026-09-23"]
    assert len(removed) == 2


def test_schedule(env):
    e, _, _ = env
    e.settings.download_time = "09:00"
    morning = dt.datetime(2026, 9, 23, 8, 59)
    later = dt.datetime(2026, 9, 23, 9, 0)
    assert not e.download_due(morning, now_ts=10_000)
    assert e.download_due(later, now_ts=10_000)
    e.state.last_download_date = "2026-09-23"
    assert not e.download_due(later, now_ts=10_000)
    assert e.next_download_at(later).date() == dt.date(2026, 9, 24)
    e.settings.refresh_rate = 3600
    e.state.last_rotation_ts = 0
    assert e.rotation_due(3600) and not e.rotation_due(3599)


def fake_downloads(e, monkeypatch, today, names):
    """Patch fetch() so each site 'downloads' a picture dated today."""
    def fake_fetch(site, ctx):
        if site not in names:
            raise sources.SourceError("offline", network=True)
        p = ctx.storage.save_bytes(site, ctx.today, names[site], "png", png(4, 2))
        return Picture(site, p, names[site], "", "u:" + names[site])
    monkeypatch.setattr(engine_mod, "fetch", fake_fetch)
    monkeypatch.setattr(engine_mod.dt, "date", type("D", (dt.date,), {"today": staticmethod(lambda: dt.date.fromisoformat(today))}))


def test_clone_yes_loops_day_pool(env, monkeypatch):
    e, be, _ = env
    add(e, "Bing", "2026-09-22", "old")
    fake_downloads(e, monkeypatch, "2026-09-23", {"Bing": "b", "NASA": "n", "Wikimedia": "w"})
    r = e.finish_downloads(e.run_downloads())
    assert len(r["pictures"]) == 3 and len(r["errors"]) == 4
    assert e.state.last_download_date == "2026-09-23"
    assert be.set == {"A|1": "2026-09-23_b.png", "B|1": "2026-09-23_b.png"}   # same everywhere
    seen = []
    for _ in range(4):
        e.rotate()
        seen.append(be.set["A|1"])
        assert be.set["A|1"] == be.set["B|1"]
    # only today's pictures, in a loop (old picture never used)
    assert seen == ["2026-09-23_n.png", "2026-09-23_w.png", "2026-09-23_b.png", "2026-09-23_n.png"]


def test_clone_no_different_per_screen_and_space(env, monkeypatch):
    e, be, _ = env
    e.settings.clone_wallpapers = False
    add(e, "Bing", "2026-09-22", "y1")
    add(e, "NASA", "2026-09-21", "y2")
    fake_downloads(e, monkeypatch, "2026-09-23", {"Bing": "t1"})
    e.finish_downloads(e.run_downloads())
    # 2 screens, only 1 picture today -> 2nd screen falls back to day-1
    assert be.set == {"A|1": "2026-09-23_t1.png", "B|1": "2026-09-22_y1.png"}
    # switch to Space 2 on screen A: gets the next unused picture (day-2)
    be.keys = ["A|2", "B|1"]
    e.apply()
    assert be.set["A|2"] == "2026-09-21_y2.png"
    # pool of 3, 3 slots: every slot still changes on rotation, wrapping to the start
    before = dict(be.set)
    e.rotate()
    assert be.set["A|2"] == "2026-09-23_t1.png"
    assert be.set["B|1"] == "2026-09-21_y2.png"
    assert all(be.set[k] != before[k] for k in ("A|2", "B|1"))


def test_set_now_override_until_rotation(env, monkeypatch):
    e, be, _ = env
    fake_downloads(e, monkeypatch, "2026-09-23", {"Bing": "a", "NASA": "b"})
    e.finish_downloads(e.run_downloads())
    special = add(e, "PicSum", "2026-09-23", "manual")
    e.set_now(special)
    assert set(be.set.values()) == {special.name}
    be.keys.append("C|1")             # a desktop you switch to later keeps its own picture
    e.apply()
    assert be.set["C|1"] != special.name
    e.rotate()
    assert special.name not in be.set.values()


def test_disabled_site_excluded(env, monkeypatch):
    e, be, _ = env
    fake_downloads(e, monkeypatch, "2026-09-23", {"Bing": "a", "NASA": "b"})
    e.finish_downloads(e.run_downloads())
    e.settings.enabled["Bing"] = False
    e.apply(force=True)
    assert set(be.set.values()) == {"2026-09-23_b.png"}


def test_offline_sources_are_retried_not_disabled(env, monkeypatch):
    e, be, _ = env
    fake_downloads(e, monkeypatch, "2026-09-23", {"Bing": "b"})
    r = e.finish_downloads(e.run_downloads())
    assert r["disabled"] == [] and len(e.settings.enabled_sites()) == 7
    assert e.retry_due() == ["NASA", "National Geographic", "Unsplash", "Wikimedia", "PicSum", "Photos"]
    # next refresh: NASA works again, the others are still offline
    fake_downloads(e, monkeypatch, "2026-09-23", {"NASA": "n"})
    e.finish_downloads(e.run_downloads(e.retry_due()), daily=False)
    assert "NASA" not in e.retry_due() and "PicSum" in e.retry_due()


def test_clone_no_one_screen_changes_every_rotation(env, monkeypatch):
    e, be, _ = env
    e.settings.clone_wallpapers = False
    be.keys = ["A|1"]
    fake_downloads(e, monkeypatch, "2026-09-23", {"Bing": "a", "NASA": "b", "PicSum": "c"})
    e.finish_downloads(e.run_downloads())
    seen = [be.set["A|1"]]
    for _ in range(3):
        e.rotate()
        seen.append(be.set["A|1"])
    assert seen == ["2026-09-23_a.png", "2026-09-23_b.png", "2026-09-23_c.png", "2026-09-23_a.png"]


def test_api_keys_and_old_unsplash_key_removed(tmp_path, monkeypatch):
    import hashlib, json
    from potd import config
    monkeypatch.setattr(config, "_OLD_UNSPLASH_KEY_SHA256", hashlib.sha256(b"old-default").hexdigest())
    (tmp_path / "s.json").write_text(json.dumps(
        {"unsplash_access_key": "old-default", "nasa_api_key": ""}))
    s = Settings.load(tmp_path / "s.json")
    assert s.unsplash_access_key == "" and s.nasa_api_key == "DEMO_KEY"
    s.set_api_key("Unsplash", "  my-key ")
    assert s.api_key("Unsplash") == "my-key" and s.api_key("Bing") is None
    # the user may enter the old key again: it must survive saving and reloading
    s.set_api_key("Unsplash", "old-default")
    s.save(tmp_path / "s.json")
    assert Settings.load(tmp_path / "s.json").unsplash_access_key == "old-default"
    assert Settings().unsplash_access_key == ""


def fake_tests(monkeypatch, results):
    def fake_test(site, ctx):
        r = results[site]
        if r is True:
            return "ok"
        raise sources.SourceError(r, network=(r == "offline"))
    monkeypatch.setattr(engine_mod, "test_source", fake_test)


def test_failed_sources_are_disabled(env, monkeypatch):
    e, _, tmp = env
    fake_tests(monkeypatch, {"Bing": True, "NASA": "HTTP 403", "National Geographic": True,
                             "Unsplash": "no key", "Wikimedia": True, "PicSum": "offline",
                             "Photos": True})
    res = e.test_sources()
    disabled, offline = e.apply_test_results(res)
    # PicSum was only unreachable (temporary): kept enabled
    assert not offline and sorted(disabled) == ["NASA", "Unsplash"]
    assert e.settings.enabled_sites() == ["Bing", "National Geographic", "Wikimedia", "PicSum", "Photos"]
    assert Settings.load(tmp / "s.json").enabled["NASA"] is False      # persisted


def test_offline_disables_nothing(env, monkeypatch):
    e, _, _ = env
    from potd.config import SITES
    fake_tests(monkeypatch, {s: "offline" for s in SITES})
    disabled, offline = e.apply_test_results(e.test_sources())
    assert offline and disabled == [] and len(e.settings.enabled_sites()) == 7


def test_test_uses_unsaved_key_and_save_enables(env, monkeypatch):
    e, _, tmp = env
    seen = {}

    def fake_test(site, ctx):
        seen[site] = ctx.unsplash_access_key
        if ctx.unsplash_access_key != "good":
            raise sources.SourceError("Unsplash rejected the Access Key")
        return "ok"
    monkeypatch.setattr(engine_mod, "test_source", fake_test)
    e.settings.enabled["Unsplash"] = False
    assert e.test_sources(["Unsplash"], {"Unsplash": "bad"})["Unsplash"][0] is False
    assert e.test_sources(["Unsplash"], {"Unsplash": " good "})["Unsplash"][0] is True
    assert seen["Unsplash"] == "good"
    assert e.settings.unsplash_access_key == ""            # testing doesn't save
    e.save_source("Unsplash", "good")
    s = Settings.load(tmp / "s.json")
    assert s.unsplash_access_key == "good" and s.enabled["Unsplash"] is True


def test_permanent_error_disables_source(env, monkeypatch):
    e, _, _ = env

    def fake_fetch(site, ctx):
        if site == "Unsplash":
            raise sources.SourceError("Unsplash rejected the Access Key")
        if site == "NASA":
            raise sources.SourceError("NASA rate limit", transient=True)
        p = ctx.storage.save_bytes(site, ctx.today, "x", "png", png(4, 2))
        return Picture(site, p)
    monkeypatch.setattr(engine_mod, "fetch", fake_fetch)
    r = e.finish_downloads(e.run_downloads())
    assert r["disabled"] == ["Unsplash"] and r["retry"] == ["NASA"]
    assert e.settings.enabled["Unsplash"] is False and e.settings.enabled["NASA"] is True


def test_daily_sources_no_network_after_first_download(tmp_path, monkeypatch):
    from potd.storage import Storage
    calls = []
    today = dt.date.today().isoformat()

    def fake_bing(ctx):
        calls.append("bing")
        ctx.source_date = ctx.today
        p = ctx.storage.save_bytes("Bing", ctx.today, "b", "png", png(4, 2))
        return Picture("Bing", p)

    def fake_picsum(ctx):
        calls.append("picsum")
        p = ctx.storage.save_bytes("PicSum", ctx.today, "p", "png", png(4, 2))
        return Picture("PicSum", p)
    monkeypatch.setitem(sources.FETCHERS, "Bing", fake_bing)
    monkeypatch.setitem(sources.FETCHERS, "PicSum", fake_picsum)
    ctx = sources.FetchContext(Storage(tmp_path), today)
    first = sources.fetch("Bing", ctx)
    again = sources.fetch("Bing", ctx)
    assert first.new and not again.new and again.path == first.path
    sources.fetch("PicSum", ctx)
    sources.fetch("PicSum", ctx)
    assert calls == ["bing", "picsum", "picsum"]      # Bing: 1 call; random source: every time


def test_daily_source_not_yet_published_is_rechecked(tmp_path, monkeypatch):
    from potd.storage import Storage
    calls = []

    def fake_bing(ctx):
        calls.append(1)
        ctx.source_date = "2026-09-22" if len(calls) == 1 else "2026-09-23"
        p = ctx.storage.save_bytes("Bing", ctx.today, f"b{len(calls)}", "png", png(4, 2))
        return Picture("Bing", p)
    monkeypatch.setitem(sources.FETCHERS, "Bing", fake_bing)
    ctx = sources.FetchContext(Storage(tmp_path), "2026-09-23")
    assert sources.fetch("Bing", ctx).stale is True      # still yesterday's -> check again
    assert sources.fetch("Bing", ctx).stale is False     # today's arrived
    sources.fetch("Bing", ctx)
    assert len(calls) == 2                               # then no more calls today


def test_refreshes_call_only_random_sources_again(env, monkeypatch):
    e, be, _ = env
    calls = []

    def maker(site, name):
        def f(ctx):
            calls.append(site)
            ctx.source_date = ctx.today
            p = ctx.storage.save_bytes(site, ctx.today, f"{name}{len(calls)}", "png", png(4, 2))
            return Picture(site, p, new=True)
        return f
    for site in ["Bing", "NASA", "National Geographic", "Unsplash", "Wikimedia", "PicSum"]:
        monkeypatch.setitem(sources.FETCHERS, site, maker(site, site[:2]))
    for _ in range(3):                      # open + 2 refreshes
        e.finish_downloads(e.run_downloads(), daily=False)
        e.rotate()
    assert calls.count("Bing") == calls.count("NASA") == 1
    assert calls.count("National Geographic") == calls.count("Wikimedia") == 1
    assert calls.count("Unsplash") == calls.count("PicSum") == 3
    assert len(e.state.day_pool) == 4 + 3 * 2     # new random pictures join today's pictures


def test_refresh_rate_in_hours():
    s = Settings()
    assert s.refresh_hours == 1
    for given, hours in ((0, 1), (5, 5), (24, 24), (99, 24), ("x", 1)):
        s.refresh_hours = given
        assert s.refresh_hours == hours and s.refresh_rate == hours * 3600
    old = Settings(refresh_rate=5400); old.normalize()      # 1.5 h from an older version
    assert old.refresh_hours == 2
    old = Settings(refresh_rate=600); old.normalize()       # 10 min -> 1 h
    assert old.refresh_hours == 1


def test_photos_source_random_private_and_no_repeats(env, monkeypatch):
    """Photos: a new random photo each refresh, stored privately, recent ones avoided."""
    import os
    import stat
    import sys
    import types
    e, be, tmp = env
    fake = types.ModuleType("potd.photos")

    class PhotosError(Exception):
        def __init__(self, msg, transient=False):
            super().__init__(msg)
            self.transient = transient
    library = ["A", "B", "C"]
    seen_avoid = []

    captions = []

    def random_photo(w, h, avoid, caption=True, place_cache=None):
        seen_avoid.append(set(avoid))
        captions.append(caption)
        place_cache["50.850,4.350"] = "Brussels, Belgium"
        pick = next(x for x in library if x not in avoid)
        return png(40, 20), pick, f"Photo {pick}", "Apple iPhone 15 Pro"
    fake.PhotosError, fake.random_photo = PhotosError, random_photo
    fake.check = lambda: "3 photos in the library"
    monkeypatch.setitem(sys.modules, "potd.photos", fake)
    import potd
    monkeypatch.setattr(potd, "photos", fake, raising=False)

    ctx = e._ctx()
    p1 = sources.fetch("Photos", ctx)
    p2 = sources.fetch("Photos", ctx)
    assert p1.path != p2.path and p1.title == "Photo A" and p2.title == "Photo B"
    assert seen_avoid[1] == {"A"}                                  # no immediate repeat
    assert p1.path.parent == tmp / "private" / "Photos"            # not in /Users/Shared
    assert stat.S_IMODE(os.stat(p1.path).st_mode) == 0o600
    assert e.storage.resolve(p1.rel).path == p1.path               # rotation can find it
    assert sources.test_source("Photos", ctx) == "3 photos in the library"
    assert captions == [True, True]                                # setting passed through
    assert e.storage.place_cache() == {"50.850,4.350": "Brussels, Belgium"}   # remembered
    assert e.caption(p1.path) == "Photo A — Apple iPhone 15 Pro"

    def denied(w, h, avoid, **kw):
        raise PhotosError("no access to Photos")
    fake.random_photo = denied
    r = e.finish_downloads(e.run_downloads(["Photos"]), daily=False)
    assert r["disabled"] == ["Photos"]                             # permission refused -> unticked


def test_photo_caption_text_and_layout():
    from potd.photo_caption import (caption_lines, fill_rect, format_camera, format_coordinates,
                                    format_place, output_size, text_metrics)
    assert format_camera("Apple", "iPhone 15 Pro") == "Apple iPhone 15 Pro"
    assert format_camera("Canon", "Canon EOS R6") == "Canon EOS R6"
    assert format_camera("NIKON CORPORATION", "NIKON Z 6") == "NIKON Z 6"
    assert format_camera("SONY", "ILCE-7M3") == "Sony ILCE-7M3"
    assert format_camera(None, None) == ""
    assert format_place("Brussels", "Brussels", "Belgium") == "Brussels, Belgium"
    assert format_place("Ghent", "East Flanders", "Belgium") == "Ghent, Belgium"
    assert format_coordinates(50.85, -4.35) == "50.8500° N, 4.3500° W"
    assert caption_lines("14 July 2023 at 18:22", "", "Apple iPhone 15 Pro") == \
        ["14 July 2023 at 18:22", "Apple iPhone 15 Pro"]          # no location -> line left out
    # output has the screen's shape, so macOS doesn't crop the corner text away
    assert output_size(4032, 3024, 2880, 1800) == (2880, 1800)
    w, h = output_size(1600, 1200, 2880, 1800)                    # small photo: no upscaling
    assert (w, h) == (1600, 1000) and abs(w / h - 2880 / 1800) < 0.01
    x, y, dw, dh = fill_rect(4032, 3024, 2880, 1800)              # covers the whole output
    assert x <= 0 and y <= 0 and dw >= 2880 and dh >= 1800
    m = text_metrics(1800)
    assert 20 <= m["font"] <= 40 and m["margin_x"] > 0 and m["margin_y"] > 0


def test_liquid_glass_style_adapts_to_the_photo():
    from potd.photo_caption import glass_geometry, glass_style, relative_luminance, text_metrics
    assert relative_luminance(1, 1, 1) == pytest.approx(1.0)
    assert relative_luminance(0, 0, 0) == 0
    bright, dark = glass_style(relative_luminance(0.9, 0.92, 0.95)), glass_style(0.03)
    assert bright["text"][0] < 0.5 and not bright["text_shadow"]    # dark text on a bright photo
    assert dark["text"][0] > 0.9 and dark["text_shadow"]            # white text on a dark photo
    m = text_metrics(1800)
    g = glass_geometry(400, 90, m)
    assert g["w"] > 400 and g["h"] > 90                             # padding around the text
    assert 0 < g["radius"] <= g["h"] / 2                            # rounded, never more than a capsule
    assert g["x"] == m["margin_x"] and g["y"] == m["margin_y"]      # bottom-left corner
