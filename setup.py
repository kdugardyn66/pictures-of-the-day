"""Build potd.app:  python setup.py py2app"""
import zlib

from setuptools import setup

from potd import __version__

# Python 3.14+ builds zlib into the interpreter, so `zlib.__file__` no longer exists,
# but py2app (<= 0.28.10) still tries to copy that file into the app. Skip that copy.
if not hasattr(zlib, "__file__"):
    from py2app import build_app

    zlib.__file__ = None
    _orig_copy_file = build_app.py2app.copy_file

    def _copy_file(self, infile, *args, **kwargs):
        if infile is None:
            return (None, 0)
        return _orig_copy_file(self, infile, *args, **kwargs)

    build_app.py2app.copy_file = _copy_file

OPTIONS = {
    "argv_emulation": False,
    "iconfile": "assets/potd.icns",
    "packages": ["potd", "certifi"],
    "includes": ["ServiceManagement", "Photos", "Quartz", "CoreLocation"],
    "plist": {
        "CFBundleName": "potd",
        "CFBundleDisplayName": "potd",
        "CFBundleIdentifier": "com.dugardyn.potd",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "LSUIElement": True,                 # menu bar only, no Dock icon
        # Shown by macOS when potd first asks for access to the Photos library.
        "NSPhotoLibraryUsageDescription":
            "potd uses random landscape photos from your Photos library as wallpaper.",
        "LSMinimumSystemVersion": "12.0",
        "NSHumanReadableCopyright": "potd — Pictures of the Day",
    },
}

setup(
    name="potd",
    version=__version__,
    app=["run_potd.py"],
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
