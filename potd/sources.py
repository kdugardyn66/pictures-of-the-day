"""Picture-of-the-day fetchers. Pure stdlib (urllib) + certifi, so they run anywhere."""
from __future__ import annotations

import datetime as dt
import json
import re
import ssl
import struct
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .storage import Storage

UA = "potd/1.0 (macOS menu bar wallpaper app)"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
MAX_BYTES = 80 * 1024 * 1024

# Sources that return a new random picture on every call (the others have one picture per day).
RANDOM_SITES = {"Unsplash", "PicSum", "Photos"}
# One picture per day: once today's is downloaded, no more network calls today.
DAILY_SITES = {"Bing", "NASA", "National Geographic", "Wikimedia"}


TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}


class SourceError(Exception):
    """`network`: the site couldn't be reached at all (offline, DNS, timeout).
    `transient`: worth retrying later (network trouble, rate limit, server error).
    Anything else (bad API key, page layout changed, ...) is permanent."""

    def __init__(self, msg, network: bool = False, transient: bool | None = None):
        super().__init__(msg)
        self.network = network
        self.transient = network if transient is None else transient


# Shown in each source's info window.
SOURCE_INFO = {
    "Bing": {"url": "https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=1",
             "note": "No API key needed."},
    "NASA": {"url": "https://api.nasa.gov/planetary/apod",
             "note": "DEMO_KEY allows ~30 requests/hour. Free personal key: https://api.nasa.gov"},
    "National Geographic": {"url": "https://www.nationalgeographic.com/photo-of-the-day",
                            "note": "No API key needed."},
    "Unsplash": {"url": "https://api.unsplash.com/photos/random",
                 "note": "Access Key of your app at https://unsplash.com/oauth/applications"},
    "Wikimedia": {"url": "https://commons.wikimedia.org/w/api.php (Template:Potd/<date>)",
                  "note": "No API key needed."},
    "Photos": {"url": "Photos library on this Mac (Photos app, incl. iCloud Photos)",
               "note": "Random landscape photo at every refresh. No API key needed."},
    "PicSum": {"url": "https://picsum.photos",
               "note": "No API key needed."},
}


@dataclass
class Picture:
    site: str
    path: Path
    title: str = ""
    credit: str = ""
    source_url: str = ""
    new: bool = True
    stale: bool = False     # the source still shows an older day's picture: check again later

    @property
    def rel(self) -> str:
        return f"{self.site}/{self.path.name}"


@dataclass
class FetchContext:
    storage: Storage
    today: str                   # YYYY-MM-DD, used in file names
    width: int = 2560
    height: int = 1440
    nasa_api_key: str = "DEMO_KEY"
    unsplash_access_key: str = ""
    bing_market: str = "en-US"
    source_date: str | None = None   # set by a fetcher: the day the source's picture is for
    photo_caption: bool = True       # Photos: write date / place / camera on the picture


# ---------------------------------------------------------------- http helpers
_SSL = None


def _ssl_ctx():
    global _SSL
    if _SSL is None:
        try:
            import certifi
            _SSL = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _SSL = ssl.create_default_context()
    return _SSL


def http_get(url: str, ua: str = UA, timeout: int = 40) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
            data = r.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:     # the server answered, with an error
        raise SourceError(f"{urllib.parse.urlsplit(url).netloc}: HTTP {e.code} {e.reason}",
                          transient=e.code in TRANSIENT_HTTP) from e
    except Exception as e:                  # URLError, timeout, ... : not reachable
        raise SourceError(f"{urllib.parse.urlsplit(url).netloc}: {getattr(e, 'reason', e)}", network=True) from e
    if len(data) > MAX_BYTES:
        raise SourceError("download too large")
    return data


def http_json(url: str, ua: str = UA):
    try:
        return json.loads(http_get(url, ua).decode("utf-8"))
    except ValueError as e:
        raise SourceError(f"bad JSON from {urllib.parse.urlsplit(url).netloc}") from e


# ---------------------------------------------------------------- image helpers
_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def image_info(data: bytes):
    """Return (width, height, ext) for JPEG/PNG data, or None if not an image."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return w, h, "png"
    if data[:2] == b"\xff\xd8":
        i, n = 2, len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker == 0xFF:
                i += 1
                continue
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seglen = struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker in _SOF:
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h, "jpg"
            i += 2 + seglen
    return None


def is_wallpaper_shaped(w: int, h: int, min_width: int = 1600) -> bool:
    return w >= min_width and w * 10 >= h * 13   # landscape, at least ~1.3:1


def slug(text: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", text or "").strip("-._")
    return (s[:maxlen].rstrip("-._") or "picture")


def _store(ctx: FetchContext, site: str, data: bytes, name: str, title: str, credit: str,
           source_url: str, require_landscape: bool = False) -> Picture:
    info = image_info(data)
    if not info:
        raise SourceError("download was not a JPEG/PNG image")
    w, h, ext = info
    if require_landscape and not is_wallpaper_shaped(w, h):
        raise SourceError(f"not wallpaper-shaped ({w}x{h})")
    path = ctx.storage.save_bytes(site, ctx.today, slug(name), ext, data)
    pic = Picture(site, path, title.strip(), credit.strip(), source_url, True)
    ctx.storage.meta_set(pic.rel, {"title": pic.title, "credit": pic.credit,
                                   "source_url": source_url, "size": [w, h]})
    return pic


def _existing(ctx: FetchContext, site: str, source_url: str) -> Picture | None:
    sp = ctx.storage.find_by_source(site, source_url)
    if not sp:
        return None
    m = ctx.storage.meta_get(sp.rel)
    return Picture(site, sp.path, m.get("title", ""), m.get("credit", ""), source_url, False)


# ---------------------------------------------------------------- sources
def fetch_bing(ctx: FetchContext) -> Picture:
    site = "Bing"
    q = urllib.parse.urlencode({"format": "js", "idx": 0, "n": 1, "mkt": ctx.bing_market})
    j = http_json(f"https://www.bing.com/HPImageArchive.aspx?{q}")
    try:
        img = j["images"][0]
    except (KeyError, IndexError, TypeError):
        raise SourceError("Bing returned no image")
    sd = str(img.get("startdate") or "")
    if len(sd) == 8:
        ctx.source_date = f"{sd[:4]}-{sd[4:6]}-{sd[6:]}"
    base = img.get("urlbase") or ""
    url = f"https://www.bing.com{base}_UHD.jpg" if base else urllib.parse.urljoin("https://www.bing.com", img["url"])
    if (p := _existing(ctx, site, url)):
        return p
    name = base.rsplit("/", 1)[-1].split("=", 1)[-1] or img.get("startdate", "bing")
    copyright_ = img.get("copyright", "")
    title = img.get("title") or copyright_.split("(")[0]
    credit = copyright_[copyright_.find("(") + 1:copyright_.rfind(")")] if "(" in copyright_ else ""
    return _store(ctx, site, http_get(url), name, title, credit, url)


def fetch_nasa(ctx: FetchContext) -> Picture:
    """Newest APOD that is an image and wallpaper-shaped (APOD is sometimes a video/portrait)."""
    site = "NASA"
    start = (dt.date.fromisoformat(ctx.today) - dt.timedelta(days=10)).isoformat()
    q = urllib.parse.urlencode({"api_key": ctx.nasa_api_key or "DEMO_KEY", "start_date": start})
    items = http_json(f"https://api.nasa.gov/planetary/apod?{q}")
    if isinstance(items, dict):
        raise SourceError(items.get("msg") or items.get("error", {}).get("message") or "NASA API error")
    reasons = []
    ctx.source_date = max((it.get("date", "") for it in items), default=None) or None
    for it in sorted(items, key=lambda x: x.get("date", ""), reverse=True):
        if it.get("media_type") != "image":
            continue
        url = it.get("hdurl") or it.get("url")
        if not url:
            continue
        if (p := _existing(ctx, site, url)):
            return p
        if ctx.storage.is_rejected(url):
            continue
        try:
            return _store(ctx, site, http_get(url), url.rsplit("/", 1)[-1].rsplit(".", 1)[0],
                          it.get("title", ""), it.get("copyright", "NASA APOD").replace("\n", " "),
                          url, require_landscape=True)
        except SourceError as e:
            reasons.append(str(e))
            if "wallpaper-shaped" in str(e):
                ctx.storage.reject(url)
    raise SourceError("no suitable APOD image in the last 10 days" + (f" ({reasons[0]})" if reasons else ""))


def _meta_tag(html: str, prop: str) -> str | None:
    for pat in (rf'<meta[^>]+property="{prop}"[^>]+content="([^"]+)"',
                rf'<meta[^>]+content="([^"]+)"[^>]+property="{prop}"'):
        m = re.search(pat, html, re.I)
        if m:
            import html as _h
            return _h.unescape(m.group(1))
    return None


def fetch_natgeo(ctx: FetchContext) -> Picture:
    site = "National Geographic"
    html = http_get("https://www.nationalgeographic.com/photo-of-the-day", BROWSER_UA).decode("utf-8", "replace")
    url = _meta_tag(html, "og:image")
    if not url:
        raise SourceError("could not find the photo on the page")
    if (p := _existing(ctx, site, url)):
        return p
    title = _meta_tag(html, "og:title") or "Photo of the Day"
    name = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return _store(ctx, site, http_get(url, BROWSER_UA), name, title, "National Geographic", url)


def fetch_unsplash(ctx: FetchContext) -> Picture:
    site = "Unsplash"
    if not ctx.unsplash_access_key:
        raise SourceError("no Unsplash access key configured")
    key = urllib.parse.quote(ctx.unsplash_access_key)
    try:
        j = http_json(f"https://api.unsplash.com/photos/random?orientation=landscape&client_id={key}")
    except SourceError as e:
        raise _unsplash_error(e) from e
    try:
        raw = j["urls"]["raw"]
    except (KeyError, TypeError):
        raise SourceError(j.get("errors", ["Unsplash API error"])[0] if isinstance(j, dict) else "Unsplash API error")
    sep = "&" if "?" in raw else "?"
    url = f"{raw}{sep}w={ctx.width}&h={ctx.height}&fit=crop&fm=jpg&q=85"
    pic = _store(ctx, site, http_get(url), j.get("slug") or j.get("id", "unsplash"),
                 j.get("description") or j.get("alt_description") or "Unsplash",
                 f"{(j.get('user') or {}).get('name', '')} / Unsplash", url)
    dl = (j.get("links") or {}).get("download_location")
    if dl:  # Unsplash API guidelines: report the download
        try:
            http_get(f"{dl}{'&' if '?' in dl else '?'}client_id={key}", timeout=10)
        except SourceError:
            pass
    return pic


def _unsplash_error(e: SourceError) -> SourceError:
    if "401" in str(e):
        return SourceError("Unsplash rejected the Access Key")
    if "403" in str(e):
        return SourceError("Unsplash rate limit reached (50 requests/hour for demo apps)", transient=True)
    return e


def fetch_wikimedia(ctx: FetchContext) -> Picture:
    site = "Wikimedia"
    today = dt.date.fromisoformat(ctx.today)
    last = None
    for back in range(3):
        d = (today - dt.timedelta(days=back)).isoformat()
        q = urllib.parse.urlencode({
            "action": "query", "format": "json", "formatversion": 2, "generator": "images",
            "titles": f"Template:Potd/{d}", "prop": "imageinfo",
            "iiprop": "url|extmetadata", "iiurlwidth": ctx.width})
        j = http_json(f"https://commons.wikimedia.org/w/api.php?{q}")
        try:
            page = j["query"]["pages"][0]
            ii = page["imageinfo"][0]
        except (KeyError, IndexError, TypeError):
            last = f"no picture of the day for {d}"
            continue
        url = ii.get("thumburl") or ii.get("url")
        ctx.source_date = d
        if (p := _existing(ctx, site, url)):
            return p
        em = ii.get("extmetadata") or {}
        artist = re.sub(r"<[^>]+>", "", (em.get("Artist") or {}).get("value", ""))
        title = page.get("title", "").removeprefix("File:").rsplit(".", 1)[0]
        return _store(ctx, site, http_get(url), title, title, artist or "Wikimedia Commons", url)
    raise SourceError(last or "Wikimedia returned nothing")


def fetch_picsum(ctx: FetchContext) -> Picture:
    site = "PicSum"
    url = f"https://picsum.photos/{min(ctx.width, 5000)}/{min(ctx.height, 5000)}"
    stamp = dt.datetime.now().strftime("%H%M%S")
    return _store(ctx, site, http_get(url), f"picsum-{stamp}", "Lorem Picsum", "picsum.photos", url)


def _photos_module():
    try:
        from . import photos
        return photos
    except ImportError as e:     # not on macOS / pyobjc-framework-Photos missing
        raise SourceError(f"Photos framework not available ({e})") from e


def fetch_photos(ctx: FetchContext) -> Picture:
    """A random landscape photo from the Photos library, rendered at screen size."""
    site = "Photos"
    ph = _photos_module()
    recent = ctx.storage.recent_photos()
    places = ctx.storage.place_cache()
    try:
        data, local_id, title, credit = ph.random_photo(ctx.width, ctx.height, set(recent),
                                                        caption=ctx.photo_caption, place_cache=places)
    except ph.PhotosError as e:
        raise SourceError(str(e), transient=e.transient) from e
    ctx.storage.add_recent_photo(local_id)
    ctx.storage.save_place_cache(places)
    stamp = dt.datetime.now().strftime("%H%M%S")
    return _store(ctx, site, data, f"photo-{stamp}", title, credit, "photos:" + local_id)


FETCHERS = {
    "Bing": fetch_bing,
    "NASA": fetch_nasa,
    "National Geographic": fetch_natgeo,
    "Unsplash": fetch_unsplash,
    "Wikimedia": fetch_wikimedia,
    "PicSum": fetch_picsum,
    "Photos": fetch_photos,
}


def cached_today(site: str, ctx: FetchContext) -> Picture | None:
    """Today's picture of a daily source, if already downloaded today (no network)."""
    if site not in DAILY_SITES:
        return None
    entry = ctx.storage.daily_entry(site)
    if entry is not None:
        if entry.get("date") != ctx.today or not entry.get("done", True):
            return None                     # not done today, or still yesterday's picture
        sp = ctx.storage.resolve(entry.get("rel", ""))
    else:   # older potd versions had no marker: a file dated today counts
        sp = next((p for p in ctx.storage.pictures(site) if p.date == ctx.today), None)
    if sp is None:
        return None
    m = ctx.storage.meta_get(sp.rel)
    return Picture(site, sp.path, m.get("title", ""), m.get("credit", ""), m.get("source_url", ""), False)


def fetch(site: str, ctx: FetchContext) -> Picture:
    if (p := cached_today(site, ctx)):
        return p                                   # no network call
    ctx.source_date = None
    pic = FETCHERS[site](ctx)
    if site in DAILY_SITES:
        if ctx.source_date and ctx.source_date < ctx.today:
            # e.g. Bing/NASA (US time) haven't published today's picture yet
            pic.stale = True
            ctx.storage.set_daily_marker(site, ctx.today, pic.rel, done=False)
        else:
            # Today is done, even when the picture itself is older
            # (e.g. NASA posted a video today and we kept the last good image).
            ctx.storage.set_daily_marker(site, ctx.today, pic.rel)
    return pic


# ---------------------------------------------------------------- connection tests
def test_source(site: str, ctx: FetchContext) -> str:
    """Quick check that a source answers and (if needed) accepts its API key.
    Downloads no picture. Returns a short OK message or raises SourceError."""
    if site == "Bing":
        q = urllib.parse.urlencode({"format": "js", "idx": 0, "n": 1, "mkt": ctx.bing_market})
        j = http_json(f"https://www.bing.com/HPImageArchive.aspx?{q}")
        if not (isinstance(j, dict) and j.get("images")):
            raise SourceError("Bing returned no image")
        return "Bing answered"
    if site == "NASA":
        key = ctx.nasa_api_key or "DEMO_KEY"
        try:
            j = http_json(f"https://api.nasa.gov/planetary/apod?api_key={urllib.parse.quote(key)}")
        except SourceError as e:
            if "403" in str(e) or "401" in str(e):
                raise SourceError("NASA rejected the API key") from e
            if "429" in str(e):
                raise SourceError("NASA rate limit reached for this key (DEMO_KEY: ~30/hour)",
                                  transient=True) from e
            raise
        if not isinstance(j, dict) or "date" not in j:
            raise SourceError((j or {}).get("msg", "unexpected NASA answer") if isinstance(j, dict) else "unexpected NASA answer")
        return f"NASA key accepted (APOD {j['date']})"
    if site == "National Geographic":
        html = http_get("https://www.nationalgeographic.com/photo-of-the-day", BROWSER_UA).decode("utf-8", "replace")
        if not _meta_tag(html, "og:image"):
            raise SourceError("photo not found on the page (site layout changed?)")
        return "National Geographic page found"
    if site == "Unsplash":
        if not ctx.unsplash_access_key:
            raise SourceError("no Unsplash Access Key entered")
        key = urllib.parse.quote(ctx.unsplash_access_key)
        try:
            j = http_json(f"https://api.unsplash.com/photos?per_page=1&client_id={key}")
        except SourceError as e:
            raise _unsplash_error(e) from e
        if not isinstance(j, list):
            raise SourceError("unexpected Unsplash answer")
        return "Unsplash key accepted"
    if site == "Wikimedia":
        j = http_json("https://commons.wikimedia.org/w/api.php?action=query&format=json&meta=siteinfo")
        if "query" not in j:
            raise SourceError("unexpected Wikimedia answer")
        return "Wikimedia answered"
    if site == "PicSum":
        j = http_json("https://picsum.photos/v2/list?limit=1")
        if not isinstance(j, list):
            raise SourceError("unexpected PicSum answer")
        return "PicSum answered"
    if site == "Photos":
        ph = _photos_module()
        try:
            return ph.check()
        except ph.PhotosError as e:
            raise SourceError(str(e), transient=e.transient) from e
    raise SourceError(f"unknown source {site}")
