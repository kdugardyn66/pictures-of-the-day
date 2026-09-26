"""Random picture from the user's Photos library (macOS Photos app), via PhotoKit.

Uses Apple's Photos framework, so it works with iCloud Photos, HEIC files and the
permission prompt ("potd would like to access your Photos"). Only landscape photos
are used, and screenshots are skipped: portrait photos make poor wallpapers.
"""
from __future__ import annotations

import random
import threading

import AppKit as AK
import Photos as PH
import Quartz as Q
from Foundation import NSMakeRect, NSDateFormatter, NSDateFormatterLongStyle, NSDateFormatterShortStyle

from .photo_caption import (caption_lines, coordinate_values, fill_rect, fit_rect, format_camera, format_coordinates,
                            format_place, glass_geometry, glass_style, screen_shapes,
                            relative_luminance, text_metrics)

_READ_WRITE = 2          # PHAccessLevelReadWrite (reading needs this level; potd never writes)
_NOT_DETERMINED, _RESTRICTED, _DENIED, _AUTHORIZED, _LIMITED = 0, 1, 2, 3, 4
_SCREENSHOT = 1 << 2     # PHAssetMediaSubtypePhotoScreenshot
MIN_WIDTH = 1200


class PhotosError(Exception):
    def __init__(self, msg, transient=False):
        super().__init__(msg)
        self.transient = transient


def _status() -> int:
    lib = PH.PHPhotoLibrary
    if hasattr(lib, "authorizationStatusForAccessLevel_"):
        return int(lib.authorizationStatusForAccessLevel_(_READ_WRITE))
    return int(lib.authorizationStatus())


def authorize(timeout: float = 120) -> None:
    """Ask for access the first time (macOS shows the prompt); raise if not allowed."""
    status = _status()
    if status == _NOT_DETERMINED:
        done, box = threading.Event(), {}

        def handler(st):
            box["status"] = int(st)
            done.set()
        lib = PH.PHPhotoLibrary

        def ask():
            # potd is a menu-bar app: bring it to the front, or macOS may not show the prompt
            AK.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            if hasattr(lib, "requestAuthorizationForAccessLevel_handler_"):
                lib.requestAuthorizationForAccessLevel_handler_(_READ_WRITE, handler)
            else:
                lib.requestAuthorization_(handler)
        if threading.current_thread() is threading.main_thread():
            ask()
        else:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(ask)             # the prompt must be requested on the main thread
        if not done.wait(timeout):
            raise PhotosError("waiting for permission to access Photos", transient=True)
        status = box.get("status", _status())
    if status in (_AUTHORIZED, _LIMITED):
        return
    if status == _RESTRICTED:
        raise PhotosError("access to Photos is restricted on this Mac")
    raise PhotosError("no access to Photos: allow potd in System Settings > "
                      "Privacy & Security > Photos")


def _images():
    opts = PH.PHFetchOptions.alloc().init()
    opts.setIncludeHiddenAssets_(False)
    return PH.PHAsset.fetchAssetsWithMediaType_options_(PH.PHAssetMediaTypeImage, opts)


def _usable(asset) -> bool:
    w, h = int(asset.pixelWidth()), int(asset.pixelHeight())
    if int(asset.mediaSubtypes()) & _SCREENSHOT:
        return False
    return w >= MIN_WIDTH and w * 10 >= h * 13          # landscape, at least ~1.3:1


def check() -> str:
    """Quick test for the info window: permission + at least one usable photo."""
    authorize()
    result = _images()
    n = int(result.count())
    if n == 0:
        raise PhotosError("the Photos library has no photos (or none are shared with potd)")
    for _ in range(min(n, 200)):
        if _usable(result.objectAtIndex_(random.randrange(n))):
            return f"{n} photos in the library"
    raise PhotosError("no landscape photos found in the Photos library")


def _jpeg_from_rep(rep) -> bytes:
    jpeg_type = getattr(AK, "NSBitmapImageFileTypeJPEG", getattr(AK, "NSJPEGFileType", 3))
    return bytes(rep.representationUsingType_properties_(jpeg_type, {AK.NSImageCompressionFactor: 0.9}))


def _original(asset):
    """(image data, properties dict) of the photo as shown in Photos (edits included)."""
    opts = PH.PHImageRequestOptions.alloc().init()
    opts.setSynchronous_(True)                     # we're on a worker thread
    opts.setNetworkAccessAllowed_(True)            # iCloud Photos: download the original
    opts.setDeliveryMode_(PH.PHImageRequestOptionsDeliveryModeHighQualityFormat)
    box = {}
    mgr = PH.PHImageManager.defaultManager()
    if hasattr(mgr, "requestImageDataAndOrientationForAsset_options_resultHandler_"):
        def handler(data, uti, orientation, info):
            box["data"], box["info"] = data, info
        mgr.requestImageDataAndOrientationForAsset_options_resultHandler_(asset, opts, handler)
    else:
        def handler(data, uti, orientation, info):
            box["data"], box["info"] = data, info
        mgr.requestImageDataForAsset_options_resultHandler_(asset, opts, handler)
    data = box.get("data")
    if data is None:
        info = box.get("info") or {}
        err = info.get(PH.PHImageErrorKey) if hasattr(info, "get") else None
        raise PhotosError(f"could not load the photo{': ' + str(err.localizedDescription()) if err else ''}",
                          transient=True)
    src = Q.CGImageSourceCreateWithData(data, None)
    if src is None:
        raise PhotosError("unsupported photo format", transient=True)
    props = Q.CGImageSourceCopyPropertiesAtIndex(src, 0, None) or {}
    return src, props


def _camera(props) -> str:
    tiff = props.get(Q.kCGImagePropertyTIFFDictionary) or {}
    return format_camera(tiff.get(Q.kCGImagePropertyTIFFMake), tiff.get(Q.kCGImagePropertyTIFFModel))


def _date_text(asset) -> str:
    created = asset.creationDate()
    if created is None:
        return ""
    f = NSDateFormatter.alloc().init()
    f.setDateStyle_(NSDateFormatterLongStyle)      # in your macOS language, e.g. "14 July 2023"
    f.setTimeStyle_(NSDateFormatterShortStyle)
    return str(f.stringFromDate_(created))


def _place(asset, cache: dict | None) -> str:
    loc = asset.location()
    if loc is None:
        return ""
    lat, lon = coordinate_values(loc.coordinate())
    key = f"{lat:.3f},{lon:.3f}"
    if cache is not None and cache.get(key):
        return cache[key]
    name = ""
    try:
        import CoreLocation as CL
        done, box = threading.Event(), {}

        def handler(placemarks, error):
            box["pm"] = placemarks
            done.set()
        geocoder = CL.CLGeocoder.alloc().init()
        geocoder.reverseGeocodeLocation_completionHandler_(loc, handler)
        if done.wait(10) and box.get("pm"):
            pm = box["pm"][0]
            name = format_place(pm.locality(), pm.administrativeArea(), pm.country(), pm.name())
        else:
            geocoder.cancelGeocode()
    except Exception:
        name = ""
    if name and cache is not None:
        cache[key] = name                          # remembered: no lookup next time
    return name or format_coordinates(lat, lon)   # offline: show coordinates


def _frame(thumb, W: int, H: int):
    """The finished picture (without text) as a Core Image image: the whole photo fitted
    in the middle, and a blurred, darker copy of it filling the rest of the screen."""
    tw, th = Q.CGImageGetWidth(thumb), Q.CGImageGetHeight(thumb)
    base = Q.CIImage.imageWithCGImage_(thumb)
    fx, fy, fw, fh = fill_rect(tw, th, W, H)
    bg = base.imageByApplyingTransform_(Q.CGAffineTransformMake(fw / tw, 0, 0, fh / th, fx, fy))
    blur = Q.CIFilter.filterWithName_("CIGaussianBlur")
    blur.setValue_forKey_(bg.imageByClampingToExtent(), "inputImage")
    blur.setValue_forKey_(max(20.0, H / 28), "inputRadius")
    tone = Q.CIFilter.filterWithName_("CIColorControls")
    tone.setValue_forKey_(blur.valueForKey_("outputImage"), "inputImage")
    tone.setValue_forKey_(-0.12, "inputBrightness")          # a bit darker, so the photo stands out
    tone.setValue_forKey_(1.1, "inputSaturation")
    tone.setValue_forKey_(1.0, "inputContrast")
    x, y, w, h = fit_rect(tw, th, W, H)
    fg = base.imageByApplyingTransform_(Q.CGAffineTransformMake(w / tw, 0, 0, h / th, x, y))
    return fg.imageByCompositingOverImage_(tone.valueForKey_("outputImage")).imageByCroppingToRect_(
        Q.CGRectMake(0, 0, W, H))


def _render(src, props, screen_w: int, screen_h: int, lines: list[str]) -> bytes:
    """Screen-shaped JPEG: the whole photo fitted on the screen (nothing cut off), a blurred
    copy of it behind, and the photo info bottom-left."""
    iw = int(props.get(Q.kCGImagePropertyPixelWidth) or 0)
    ih = int(props.get(Q.kCGImagePropertyPixelHeight) or 0)
    orient = int(props.get(Q.kCGImagePropertyOrientation) or 1)
    if orient in (5, 6, 7, 8):                     # rotated 90°: width and height swap
        iw, ih = ih, iw
    if not iw or not ih:
        raise PhotosError("photo has no size information", transient=True)
    W, H = int(screen_w), int(screen_h)
    scale = min(W / iw, H / ih, 1.0)               # fit; never decode larger than the original
    thumb = Q.CGImageSourceCreateThumbnailAtIndex(src, 0, {
        Q.kCGImageSourceCreateThumbnailFromImageAlways: True,
        Q.kCGImageSourceCreateThumbnailWithTransform: True,        # apply the orientation
        Q.kCGImageSourceThumbnailMaxPixelSize: int(max(iw, ih) * scale) + 2,
    })
    if thumb is None:
        raise PhotosError("could not decode the photo", transient=True)
    frame = _frame(thumb, W, H)
    frame_cg = Q.CIContext.contextWithOptions_(None).createCGImage_fromRect_(frame, Q.CGRectMake(0, 0, W, H))
    if frame_cg is None:
        raise PhotosError("could not render the photo", transient=True)

    rep = AK.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, W, H, 8, 4, True, False, AK.NSDeviceRGBColorSpace, 0, 0)
    ctx = AK.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    AK.NSGraphicsContext.saveGraphicsState()
    try:
        AK.NSGraphicsContext.setCurrentContext_(ctx)
        cg = ctx.CGContext()
        Q.CGContextSetInterpolationQuality(cg, Q.kCGInterpolationHigh)
        Q.CGContextDrawImage(cg, Q.CGRectMake(0, 0, W, H), frame_cg)
        if lines:
            ctx.flushGraphics()                  # so the backdrop brightness can be read
            _draw_caption(lines, rep, cg, frame, H)
        ctx.flushGraphics()
    finally:
        AK.NSGraphicsContext.restoreGraphicsState()
    return _jpeg_from_rep(rep)


def _backdrop_luminance(rep, g: dict, H: int) -> float:
    """Average brightness of the photo where the glass panel goes (sampled grid)."""
    total, n = 0.0, 0
    cs = AK.NSColorSpace.sRGBColorSpace()
    for i in range(12):
        for j in range(6):
            x = int(g["x"] + g["w"] * (i + 0.5) / 12)
            y_top = int(H - (g["y"] + g["h"] * (j + 0.5) / 6))    # colorAtX_y_ counts from the top
            c = rep.colorAtX_y_(x, y_top)
            c = c.colorUsingColorSpace_(cs) if c is not None else None
            if c is not None:
                total += relative_luminance(c.redComponent(), c.greenComponent(), c.blueComponent())
                n += 1
    return total / n if n else 0.3


def _frosted(frame, g: dict, style: dict):
    """What's behind the panel, blurred and slightly brighter/more saturated."""
    blur = Q.CIFilter.filterWithName_("CIGaussianBlur")
    blur.setValue_forKey_(frame.imageByClampingToExtent(), "inputImage")
    blur.setValue_forKey_(g["blur"], "inputRadius")
    color = Q.CIFilter.filterWithName_("CIColorControls")
    color.setValue_forKey_(blur.valueForKey_("outputImage"), "inputImage")
    color.setValue_forKey_(style["saturation"], "inputSaturation")
    color.setValue_forKey_(style["brightness"], "inputBrightness")
    color.setValue_forKey_(1.0, "inputContrast")
    out = color.valueForKey_("outputImage")
    return Q.CIContext.contextWithOptions_(None).createCGImage_fromRect_(
        out, Q.CGRectMake(g["x"], g["y"], g["w"], g["h"]))


def _draw_caption(lines: list[str], rep, cg, frame, H: int) -> None:
    """Liquid Glass style panel with the photo info, bottom-left."""
    m = text_metrics(H)
    fonts = [AK.NSFont.systemFontOfSize_weight_(m["font"] * (1.0 if i == 0 else 0.85),
                                                AK.NSFontWeightSemibold if i == 0 else AK.NSFontWeightMedium)
             for i in range(len(lines))]
    plain = [AK.NSAttributedString.alloc().initWithString_attributes_(t, {AK.NSFontAttributeName: f})
             for t, f in zip(lines, fonts)]
    sizes = [t.size() for t in plain]
    text_w = max(sz.width for sz in sizes)
    text_h = sum(sz.height for sz in sizes) + m["line_gap"] * (len(lines) - 1)
    g = glass_geometry(text_w, text_h, m)
    style = glass_style(_backdrop_luminance(rep, g, H))
    rect = NSMakeRect(g["x"], g["y"], g["w"], g["h"])
    panel = AK.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, g["radius"], g["radius"])

    # 1. soft shadow that lifts the panel off the photo
    AK.NSGraphicsContext.saveGraphicsState()
    sh = AK.NSShadow.alloc().init()
    sh.setShadowColor_(AK.NSColor.colorWithCalibratedWhite_alpha_(0, 0.30))
    sh.setShadowBlurRadius_(g["shadow_blur"])
    sh.setShadowOffset_((0, g["shadow_y"]))
    sh.set()
    AK.NSColor.colorWithCalibratedWhite_alpha_(0, 0.10).setFill()
    panel.fill()
    AK.NSGraphicsContext.restoreGraphicsState()

    # 2. frosted glass: blurred photo + light tint + a faint sheen on the upper half
    AK.NSGraphicsContext.saveGraphicsState()
    panel.addClip()
    frosted = _frosted(frame, g, style)
    if frosted is not None:
        Q.CGContextDrawImage(cg, Q.CGRectMake(g["x"], g["y"], g["w"], g["h"]), frosted)
    AK.NSColor.colorWithCalibratedWhite_alpha_(*style["tint"]).setFill()
    panel.fill()
    sheen = AK.NSGradient.alloc().initWithStartingColor_endingColor_(
        AK.NSColor.colorWithCalibratedWhite_alpha_(1, 0.0), AK.NSColor.colorWithCalibratedWhite_alpha_(1, 0.14))
    sheen.drawInRect_angle_(rect, 90)            # brighter towards the top, like light on glass
    AK.NSGraphicsContext.restoreGraphicsState()

    # 3. rim: bright along the top edge, fading towards the bottom
    inset = g["rim"]
    inner = AK.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        NSMakeRect(g["x"] + inset, g["y"] + inset, g["w"] - 2 * inset, g["h"] - 2 * inset),
        max(0, g["radius"] - inset), max(0, g["radius"] - inset))
    ring = AK.NSBezierPath.bezierPath()
    ring.appendBezierPath_(panel)
    ring.appendBezierPath_(inner)
    ring.setWindingRule_(AK.NSWindingRuleEvenOdd)
    rim_w, rim_a = style["rim"]
    rim = AK.NSGradient.alloc().initWithStartingColor_endingColor_(
        AK.NSColor.colorWithCalibratedWhite_alpha_(rim_w, rim_a * 0.15),
        AK.NSColor.colorWithCalibratedWhite_alpha_(rim_w, rim_a))
    rim.drawInBezierPath_angle_(ring, 90)

    # 4. text, light or dark depending on what's behind the glass
    attrs_color = AK.NSColor.colorWithCalibratedWhite_alpha_(*style["text"])
    shadow = None
    if style["text_shadow"]:
        shadow = AK.NSShadow.alloc().init()
        shadow.setShadowColor_(AK.NSColor.colorWithCalibratedWhite_alpha_(0, 0.35))
        shadow.setShadowBlurRadius_(m["font"] * 0.15)
        shadow.setShadowOffset_((0, -m["font"] * 0.04))
    y = g["text_y"] + text_h                     # draw top-down (AppKit's y goes up)
    for t, f, sz in zip(lines, fonts, sizes):
        attrs = {AK.NSFontAttributeName: f, AK.NSForegroundColorAttributeName: attrs_color}
        if shadow is not None:
            attrs[AK.NSShadowAttributeName] = shadow
        y -= sz.height
        AK.NSAttributedString.alloc().initWithString_attributes_(t, attrs).drawAtPoint_((g["text_x"], y))
        y -= m["line_gap"]


def random_photo(width: int, height: int, avoid: set[str] | None = None, tries: int = 200,
                 caption: bool = True, place_cache: dict | None = None, sizes=None):
    """Pick a random landscape photo, render it screen-sized as JPEG, optionally with
    date / place / camera in the bottom-left corner.
    Returns (jpeg_bytes, local_identifier, title, credit)."""
    authorize()
    result = _images()
    n = int(result.count())
    if n == 0:
        raise PhotosError("the Photos library has no photos (or none are shared with potd)")
    avoid = avoid or set()
    asset = None
    for i in range(min(tries, n * 3)):
        a = result.objectAtIndex_(random.randrange(n))
        if _usable(a) and (str(a.localIdentifier()) not in avoid or i > tries // 2):
            asset = a
            break
    if asset is None:
        raise PhotosError("no landscape photos found in the Photos library")

    src, props = _original(asset)
    date_text, place, camera = _date_text(asset), _place(asset, place_cache), _camera(props)
    lines = caption_lines(date_text, place, camera) if caption else []
    shapes = screen_shapes(sizes or [(width, height)])
    main = shapes[0]                               # largest screen: the file in the rotation
    data = _render(src, props, main[0], main[1], lines)
    extra = {s: _render(src, props, s[0], s[1], lines) for s in shapes[1:]}   # other monitors
    title = " · ".join(x for x in (date_text, place) if x) or "Photo"
    return data, str(asset.localIdentifier()), title, camera or "Photos library", extra
