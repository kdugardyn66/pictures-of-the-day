# potd — Pictures of the Day

A macOS menu bar app that downloads the picture of the day from **Bing, NASA, National Geographic,
Unsplash, Wikimedia and PicSum**, adds random photos from your own **Photos** library, stores it in `/Users/Shared/Pictures/potd/<Site>/`, and rotates
your wallpapers across every monitor and every desktop (Space).

## Why Python

Python with **PyObjC** calls the native macOS APIs directly: `NSStatusItem` for the menu bar icon,
`NSWorkspace.setDesktopImageURL(forScreen:)` for per-monitor wallpapers, and Space-change
notifications. **py2app** then packages it as a normal `potd.app`. A JavaScript version would need
Electron (~200 MB runtime) and AppleScript calls to set wallpapers.

## Install

Requirements: macOS 12+ and Python 3.10–3.13 (recommended: `brew install python@3.13`).
`build.sh` picks the newest of 3.13/3.12/3.11/3.10 it finds, and falls back to `python3`.
Python 3.14 also works via a small py2app patch in `setup.py`. To choose one yourself:
`PYTHON=/opt/homebrew/bin/python3.13 ./build.sh`.

```bash
cd potd
./build.sh            # makes a venv, builds dist/potd.app, copies it to /Applications and opens it
```

Run without building (for development): `pip install -r requirements.txt && python3 -m potd`.

On first launch macOS may say the app is from an unidentified developer. Right-click
`potd.app` → **Open** once.

## Using it

* **Menu bar icon** (photo icon): Open potd…, Next Wallpaper, Download Pictures Now, Open Pictures
  Folder, **Quit potd**. If the icon isn't in the menu bar, potd isn't running.
* **Start from Applications**: opens the window. Opening it again while it runs brings the window
  back. Closing the window keeps potd in the menu bar. **Quit** unloads it completely.
* **Save and Close**: saves every change on the main screen (source ticks and settings) and closes
  the window. The **red close button** (or Cmd-W) closes the window and throws the changes away.
* **Dock icon**: shown while the potd window is open (click it to bring the window forward).
  Closing the window removes it from the Dock; the menu bar icon stays. Quit removes both.
* **Sources**: the checkbox enables a site for the daily download and rotation (saved with
  **Save and Close**). Click the site
  name to fetch its picture now and preview it, then press **Set Wallpaper Now**. The picture
  stays until the next refresh.
* **ⓘ info button** after each source: shows the source's URL and, for NASA and Unsplash, an
  editable API key.
  * *NASA / Unsplash*: the saved key is tested as soon as the window opens. If it works, **Test**
    is greyed out and **Save** is available. Editing the key turns **Test** on and **Save** off
    until the new key passes the test.
  * *Bing, National Geographic, Wikimedia, PicSum*: the URL can't be changed, so **Test** is
    greyed out and **Save** is always available (it quickly checks the site can be reached).
  * **Save** stores the key, **always enables** the source (even if you unticked it) and closes the
    window. Esc or the red close button closes without saving.
* **Downloading on open**: every time potd is started or opened (Applications, Spotlight,
  `open -a potd` in Terminal, or **Open potd…** in the menu bar), the pictures of all enabled
  sources are downloaded and **Bing** is shown first in the preview.
* **Less network traffic**: Bing, NASA, National Geographic and Wikimedia have one picture per day.
  Once today's is downloaded, potd makes no more calls to them until tomorrow. (If Bing or NASA,
  which publish on US time, still show yesterday's picture, they're checked again at the next
  refresh.)
* **Every refresh** (*Refresh rate*, in hours): Unsplash, PicSum and Photos each add a new random
  picture, the daily sources make no call once they have today's picture, then the wallpaper
  moves to the next one.
* **Photos** (your Photos library): the first time, macOS asks *"potd would like to access your
  Photos"*. Choose **Allow Full Access** (with *Limited Access* potd only sees the photos you pick).
  Each refresh takes a random **landscape** photo (portrait photos and screenshots are skipped;
  recently used photos aren't picked again soon). iCloud Photos works too: the original is
  downloaded when needed. Changed your mind? System Settings → Privacy & Security → Photos → potd.
  **Photo info on wallpaper** (Settings, on by default): the bottom-left corner shows when the
  photo was taken, where (place name looked up from the photo's GPS position; coordinates if
  offline) and with which camera, on a panel in the macOS **Liquid Glass** style: the photo behind
  it is frosted, the top edge catches the light, and the text turns dark on bright photos and
  white on dark ones. (A picture file can't hold the live system material, so potd draws its
  look into the wallpaper.) Lines without information are left out. The photo is cropped to
  your screen's shape so the text isn't cut off. Changing the setting applies to new photos.
  The copies are stored privately in `~/Pictures/potd/Photos` (only your account can read them),
  not in the shared folder, and are cleaned up by *Keep wallpapers* like the other sources.
* **When a source fails**:
  * *temporarily* (no connection, timeout, rate limit, server error): it stays enabled and is
    tried again at every refresh until it works;
  * *permanently* (API key rejected, page changed): it is unticked. Fix it with the ⓘ window.
  The status line and the log show which sources failed and why.

| Setting | Meaning |
|---|---|
| Download wallpapers at | Each day at this time, every enabled site is downloaded. If the Mac was asleep or off, this runs as soon as it's back. Sources that fail temporarily are retried at every refresh. |
| Refresh rate (hours) | How often the wallpaper changes: 1 to 24 hours, set with the arrows or by typing. |
| Clone wallpapers = Yes | Every monitor and every desktop shows the same picture, looping through **today's** pictures only. |
| Clone wallpapers = No | Every monitor/desktop shows a different picture: today's first, then day-1, day-2, … Once all are used it starts again from the first picture of today. Every refresh moves each screen on to the next picture. |
| Keep wallpapers (days) | Per site, pictures from the newest *N* days are kept and older ones are deleted. |
| Start potd at login | Registers potd as a login item (only in the built app, macOS 13+). |
| Photo info on wallpaper | Photos source: date, place and camera in the bottom-left corner. |

## How desktops (Spaces) are handled

macOS only lets an app set the wallpaper of the desktop that's currently visible on each monitor.
potd watches for desktop switches: the first time you switch to a desktop after a change, it gets
its picture right away. For this, potd reads the current Space ID through a private but long-stable
window-server call. If a future macOS removes that call, potd falls back to one picture per monitor.

## Files

| Path | What |
|---|---|
| `/Users/Shared/Pictures/potd/<Site>/YYYY-MM-DD_name.jpg` | pictures (date = the day it was downloaded) |
| `~/Pictures/potd/Photos/` | copies of your own photos (private to your account) |
| `/Users/Shared/Pictures/potd/.potd-meta.json` | titles and credits shown in the preview |
| `~/Library/Application Support/potd/settings.json` | settings, including API keys (`nasa_api_key`, `unsplash_access_key`, `bing_market`) |
| `~/Library/Application Support/potd/state.json` | rotation position, last download |
| `~/Library/Logs/potd.log` | log |

API keys are entered with the ⓘ button. NASA uses `DEMO_KEY` by default (about 30 requests an
hour; free personal key at https://api.nasa.gov). Unsplash has no default key: enter the Access
Key of your own Unsplash app (https://unsplash.com/oauth/applications).

## Source notes

* **Bing**: today's image in UHD from `HPImageArchive` (market set by `bing_market`).
* **NASA**: newest APOD that is a landscape image of at least 1600 px. Videos and portrait images
  are skipped, so on those days you get the previous suitable one.
* **National Geographic**: `og:image` from the Photo of the Day page (can break if the site changes).
* **Unsplash / PicSum**: random pictures. Each click fetches a new one, sized to your largest screen.
* **Wikimedia**: Commons picture of the day (`Template:Potd/<date>`).

## Tests

`python3 -m pytest tests -q` runs the scheduling, rotation, clone/no-clone and pruning logic without
needing macOS.
