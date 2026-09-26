"""macOS glue: screens, Spaces, setting wallpapers, login item."""
from __future__ import annotations

import ctypes
import logging
from pathlib import Path

import objc
import AppKit as AK
from Foundation import NSURL

log = logging.getLogger("potd")

_CG = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
_SKY = "/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight"
_CF = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_COLORSYNC = "/System/Library/Frameworks/ColorSync.framework/ColorSync"

_cf = ctypes.CDLL(_CF)
_cf.CFRelease.argtypes = [ctypes.c_void_p]
_cf.CFRelease.restype = None
_cf.CFUUIDCreateString.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_cf.CFUUIDCreateString.restype = ctypes.c_void_p

_spaces_fns = None
_uuid_fn = None


def _load_spaces_fns():
    """Private (but long-stable) window-server calls used to identify the active Space."""
    global _spaces_fns
    if _spaces_fns is None:
        _spaces_fns = False
        for path, prefix in ((_CG, "CGS"), (_SKY, "SLS")):
            try:
                lib = ctypes.CDLL(path)
                main = getattr(lib, prefix + "MainConnectionID")
                main.argtypes, main.restype = [], ctypes.c_int
                copy = getattr(lib, prefix + "CopyManagedDisplaySpaces")
                copy.argtypes, copy.restype = [ctypes.c_int], ctypes.c_void_p
                _spaces_fns = (main, copy)
                break
            except (OSError, AttributeError):
                continue
    return _spaces_fns or None


def current_spaces() -> dict[str, int]:
    """{display UUID (upper-case) or 'MAIN': id of the Space currently shown}."""
    fns = _load_spaces_fns()
    if not fns:
        return {}
    try:
        ptr = fns[1](fns[0]())
        if not ptr:
            return {}
        arr = objc.objc_object(c_void_p=ptr)
        out = {}
        for d in arr:
            ident = str(d.objectForKey_("Display Identifier") or "Main").upper()
            cur = d.objectForKey_("Current Space")
            sid = cur.objectForKey_("id64") or cur.objectForKey_("ManagedSpaceID") if cur else 0
            out[ident] = int(sid or 0)
        _cf.CFRelease(ptr)
        return out
    except Exception as e:           # never let this break wallpaper setting
        log.warning("spaces lookup failed: %s", e)
        return {}


def screen_uuid(screen) -> str | None:
    global _uuid_fn
    try:
        if _uuid_fn is None:
            lib = ctypes.CDLL(_COLORSYNC)
            _uuid_fn = lib.CGDisplayCreateUUIDFromDisplayID
            _uuid_fn.argtypes, _uuid_fn.restype = [ctypes.c_uint32], ctypes.c_void_p
        did = int(screen.deviceDescription()["NSScreenNumber"])
        u = _uuid_fn(did)
        if not u:
            return None
        s = _cf.CFUUIDCreateString(None, u)
        text = str(objc.objc_object(c_void_p=s)).upper()
        _cf.CFRelease(s)
        _cf.CFRelease(u)
        return text
    except Exception as e:
        log.warning("screen uuid failed: %s", e)
        return None


class MacBackend:
    """Implements engine.Backend with NSWorkspace/NSScreen."""

    _warned = False

    def slots(self):
        spaces = current_spaces()
        if not spaces and not MacBackend._warned:
            MacBackend._warned = True
            log.warning("could not read Space IDs: every desktop of a monitor counts as one slot")
        fallback = spaces.get("MAIN") or (next(iter(spaces.values())) if spaces else 0)
        out = []
        for i, scr in enumerate(AK.NSScreen.screens() or []):
            uid = screen_uuid(scr) or f"screen{i}"
            sid = spaces.get(uid, fallback)
            out.append((f"{uid}|{sid}", scr))
        log.debug("slots: %s", [k for k, _ in out])
        return out

    def set_wallpaper(self, screen, path: Path, fit: bool = False) -> bool:
        """fit=True: the whole picture stays visible ("Fit to Screen"); otherwise it fills
        the screen and macOS may crop the edges ("Fill Screen")."""
        url = NSURL.fileURLWithPath_(str(path))
        opts = {
            AK.NSWorkspaceDesktopImageScalingKey: AK.NSImageScaleProportionallyUpOrDown,
            AK.NSWorkspaceDesktopImageAllowClippingKey: not fit,
        }
        if fit:
            opts[AK.NSWorkspaceDesktopImageFillColorKey] = AK.NSColor.blackColor()
        ok, err = AK.NSWorkspace.sharedWorkspace().setDesktopImageURL_forScreen_options_error_(
            url, screen, opts, None)
        if not ok:
            log.warning("setting wallpaper failed: %s", err)
        return bool(ok)

    @staticmethod
    def screen_size(screen) -> tuple[int, int]:
        f, k = screen.frame().size, screen.backingScaleFactor()
        return int(f.width * k), int(f.height * k)

    def screen_sizes(self) -> list[tuple[int, int]]:
        """Pixel size of every connected monitor (Retina screens count their real pixels)."""
        return [self.screen_size(s) for s in (AK.NSScreen.screens() or [])]

    def target_size(self):
        w, h = 2560, 1440
        best = 0
        for scr in AK.NSScreen.screens() or []:
            f, k = scr.frame().size, scr.backingScaleFactor()
            sw, sh = int(f.width * k), int(f.height * k)
            if sw * sh > best:
                best, w, h = sw * sh, sw, sh
        return min(max(w, 1920), 5000), min(max(h, 1080), 5000)


def set_login_item(enabled: bool) -> tuple[bool, str]:
    try:
        from ServiceManagement import SMAppService
    except ImportError:
        return False, "Start at login needs macOS 13+ and the bundled potd.app"
    svc = SMAppService.mainAppService()
    if enabled:
        ok, err = svc.registerAndReturnError_(None)
    else:
        ok, err = svc.unregisterAndReturnError_(None)
    return bool(ok), ("" if ok else str(err.localizedDescription() if err else "failed"))
