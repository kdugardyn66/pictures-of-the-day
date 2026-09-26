"""potd menu bar app (AppKit via PyObjC)."""
from __future__ import annotations

import datetime as dt
import logging
import struct
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import objc
import AppKit as AK
from Foundation import NSObject, NSTimer, NSCalendar, NSDate, NSURL, NSMakeRect
from PyObjCTools import AppHelper

from . import __version__
from .config import API_KEY_FIELDS, SITES, Settings, State
from .engine import Engine
from .macos import MacBackend, set_login_item
from .sources import SOURCE_INFO

log = logging.getLogger("potd")

ON, OFF = AK.NSControlStateValueOn, AK.NSControlStateValueOff
W, H = 860, 684


def _fourcc(s: str) -> int:
    return struct.unpack(">I", s.encode())[0]


def _label(text, frame, size=13, bold=False, secondary=False):
    tf = AK.NSTextField.labelWithString_(text)
    tf.setFrame_(frame)
    tf.setFont_(AK.NSFont.boldSystemFontOfSize_(size) if bold else AK.NSFont.systemFontOfSize_(size))
    if secondary:
        tf.setTextColor_(AK.NSColor.secondaryLabelColor())
    tf.setLineBreakMode_(AK.NSLineBreakByTruncatingTail)
    return tf


def _button(title, frame, target, action):
    b = AK.NSButton.buttonWithTitle_target_action_(title, target, action)
    b.setFrame_(frame)
    return b


def _fmt_secs(s: int) -> str:
    s = max(0, int(s))
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{round(s / 60)} min"
    return f"{s / 3600:.1f} h"


class InfoPanel(AK.NSPanel):
    """Source info window. Esc (or the red close button) closes it without saving."""

    def cancelOperation_(self, sender):
        self.orderOut_(None)


class AppDelegate(NSObject):

    # ================================================================ lifecycle
    def applicationWillFinishLaunching_(self, note):
        self.launched_at_login = False
        try:
            ev = AK.NSAppleEventManager.sharedAppleEventManager().currentAppleEvent()
            if ev is not None and ev.eventID() == _fourcc("oapp"):
                prop = ev.paramDescriptorForKeyword_(_fourcc("prdt"))
                self.launched_at_login = bool(prop and prop.enumCodeValue() == _fourcc("lgit"))
        except Exception:
            pass

    def applicationDidFinishLaunching_(self, note):
        AK.NSApplication.sharedApplication().setActivationPolicy_(AK.NSApplicationActivationPolicyAccessory)
        self.settings = Settings.load()
        self.state = State.load()
        self.engine = Engine(self.settings, self.state, MacBackend())
        self.busy = set()               # sites being fetched
        self.downloading = False
        self.preview_path = None
        self.testing = False
        self.last_test_ts = 0.0
        self.infoSite = None

        self.buildMainMenu()
        self.buildStatusItem()
        self.buildWindow()
        self.loadSettingsIntoControls()

        ws_nc = AK.NSWorkspace.sharedWorkspace().notificationCenter()
        ws_nc.addObserver_selector_name_object_(self, "spaceChanged:", AK.NSWorkspaceActiveSpaceDidChangeNotification, None)
        ws_nc.addObserver_selector_name_object_(self, "didWake:", AK.NSWorkspaceDidWakeNotification, None)
        AK.NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self, "screensChanged:", AK.NSApplicationDidChangeScreenParametersNotification, None)

        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            20.0, self, "tick:", None, True)
        self.performSelector_withObject_afterDelay_("tick:", None, 1.5)

        if not self.launched_at_login:
            self.showWindow_(None)      # also downloads all sources
        else:
            self.openDownloads()

    def applicationShouldHandleReopen_hasVisibleWindows_(self, app, flag):
        self.showWindow_(None)          # potd.app opened again from Applications / Finder
        return True

    def applicationShouldTerminateAfterLastWindowClosed_(self, app):
        return False                    # closing the window keeps potd in the menu bar

    # ================================================================ menus
    def buildMainMenu(self):
        # Invisible for a menu-bar app, but gives text fields Cmd-C/V/X/A/Z and Cmd-W/Q.
        main = AK.NSMenu.alloc().init()
        app_item = main.addItemWithTitle_action_keyEquivalent_("potd", None, "")
        app_menu = AK.NSMenu.alloc().init()
        app_menu.addItemWithTitle_action_keyEquivalent_("Close Window", "performClose:", "w")
        app_menu.addItemWithTitle_action_keyEquivalent_("Quit potd", "terminate:", "q")
        app_item.setSubmenu_(app_menu)
        edit_item = main.addItemWithTitle_action_keyEquivalent_("Edit", None, "")
        edit = AK.NSMenu.alloc().initWithTitle_("Edit")
        for t, sel, k in (("Undo", "undo:", "z"), ("Cut", "cut:", "x"), ("Copy", "copy:", "c"),
                          ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")):
            edit.addItemWithTitle_action_keyEquivalent_(t, sel, k)
        edit_item.setSubmenu_(edit)
        AK.NSApplication.sharedApplication().setMainMenu_(main)

    def buildStatusItem(self):
        self.statusItem = AK.NSStatusBar.systemStatusBar().statusItemWithLength_(AK.NSVariableStatusItemLength)
        btn = self.statusItem.button()
        img = None
        if hasattr(AK.NSImage, "imageWithSystemSymbolName_accessibilityDescription_"):
            img = AK.NSImage.imageWithSystemSymbolName_accessibilityDescription_("photo.on.rectangle.angled", "potd")
        if img:
            img.setTemplate_(True)
            btn.setImage_(img)
        else:
            btn.setTitle_("potd")
        btn.setToolTip_("potd — Pictures of the Day")

        menu = AK.NSMenu.alloc().init()
        self.menuStatus = menu.addItemWithTitle_action_keyEquivalent_("", None, "")
        self.menuStatus.setEnabled_(False)
        menu.addItem_(AK.NSMenuItem.separatorItem())
        for title, sel, key in (("Open potd…", "showWindow:", "o"),
                                ("Next Wallpaper", "nextWallpaper:", "n"),
                                ("Download Pictures Now", "downloadNow:", "d"),
                                ("Open Pictures Folder", "openFolder:", ""),
                                (None, None, None),
                                ("Quit potd", "quit:", "q")):
            if title is None:
                menu.addItem_(AK.NSMenuItem.separatorItem())
                continue
            item = menu.addItemWithTitle_action_keyEquivalent_(title, sel, key)
            item.setTarget_(self)
        menu.setDelegate_(self)
        self.statusItem.setMenu_(menu)

    def menuWillOpen_(self, menu):
        text = self.statusText(short=True)
        off = [x for x in self.engine.state.disabled_reasons if not self.settings.enabled.get(x)]
        if off:
            text += " · unticked: " + ", ".join(off)
        self.menuStatus.setTitle_(text)

    # ================================================================ window
    def buildWindow(self):
        style = (AK.NSWindowStyleMaskTitled | AK.NSWindowStyleMaskClosable | AK.NSWindowStyleMaskMiniaturizable)
        win = AK.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), style, AK.NSBackingStoreBuffered, False)
        win.setTitle_("potd — Pictures of the Day")
        win.setReleasedWhenClosed_(False)
        win.center()
        win.setFrameAutosaveName_("potdMainWindow")
        win.setContentSize_((W, H))              # a saved frame from an older, smaller window
        win.setDelegate_(self)                   # -> windowWillClose_ (removes the Dock icon)
        self.window = win
        v = win.contentView()

        # ---- sources (left column)
        v.addSubview_(_label("Sources", NSMakeRect(20, H - 44, 250, 22), 15, bold=True))
        v.addSubview_(_label("Tick = used for the daily download. Click a name to load its picture.",
                             NSMakeRect(20, H - 82, 250, 34), 11, secondary=True))
        self.label_hint = v.subviews()[-1]
        self.label_hint.setLineBreakMode_(AK.NSLineBreakByWordWrapping)
        self.siteChecks, self.siteButtons = [], []
        for i, site in enumerate(SITES):
            y = H - 125 - i * 44
            cb = AK.NSButton.checkboxWithTitle_target_action_("", None, None)   # saved with Save and Close
            cb.setFrame_(NSMakeRect(20, y + 4, 24, 22))
            cb.setTag_(i)
            b = _button(site, NSMakeRect(44, y, 196, 32), self, "siteClicked:")
            b.setTag_(i)
            info = self.makeInfoButton(NSMakeRect(244, y + 4, 26, 24))
            info.setTag_(i)
            v.addSubview_(cb)
            v.addSubview_(b)
            v.addSubview_(info)
            self.siteChecks.append(cb)
            self.siteButtons.append(b)
        self.dlButton = _button("Download All Now", NSMakeRect(44, H - 125 - len(SITES) * 44 - 4, 226, 32), self, "downloadNow:")
        v.addSubview_(self.dlButton)

        # ---- preview (right)
        iv = AK.NSImageView.alloc().initWithFrame_(NSMakeRect(300, 300, 540, H - 320))
        iv.setImageFrameStyle_(AK.NSImageFrameGrayBezel)
        iv.setImageScaling_(AK.NSImageScaleProportionallyUpOrDown)
        v.addSubview_(iv)
        self.imageView = iv
        self.caption = _label("Click a source to preview its picture of the day.",
                              NSMakeRect(300, 272, 540, 22), 12, secondary=True)
        v.addSubview_(self.caption)
        self.setNowButton = _button("Set Wallpaper Now", NSMakeRect(296, 232, 150, 32), self, "setWallpaperNow:")
        self.setNowButton.setKeyEquivalent_("\r")
        self.setNowButton.setEnabled_(False)
        v.addSubview_(self.setNowButton)
        v.addSubview_(_button("Next Wallpaper", NSMakeRect(452, 232, 130, 32), self, "nextWallpaper:"))
        v.addSubview_(_button("Open Folder", NSMakeRect(588, 232, 120, 32), self, "openFolder:"))
        v.addSubview_(_button("Show in Finder", NSMakeRect(714, 232, 130, 32), self, "revealPreview:"))

        # ---- status line
        self.statusLabel = _label("", NSMakeRect(20, 196, W - 40, 20), 12, secondary=True)
        v.addSubview_(self.statusLabel)

        # ---- settings box
        box = AK.NSBox.alloc().initWithFrame_(NSMakeRect(16, 58, W - 32, 132))
        box.setTitle_("Settings")
        v.addSubview_(box)
        c = box.contentView()

        c.addSubview_(_label("Download wallpapers at", NSMakeRect(12, 62, 170, 22)))
        dp = AK.NSDatePicker.alloc().initWithFrame_(NSMakeRect(186, 60, 90, 26))
        dp.setDatePickerStyle_(AK.NSDatePickerStyleTextFieldAndStepper)
        dp.setDatePickerElements_(AK.NSDatePickerElementFlagHourMinute)
        c.addSubview_(dp)
        self.timePicker = dp

        c.addSubview_(_label("Refresh rate (hours)", NSMakeRect(320, 62, 170, 22)))
        self.refreshField = AK.NSTextField.alloc().initWithFrame_(NSMakeRect(490, 61, 60, 24))
        c.addSubview_(self.refreshField)
        self.refreshStepper = self.makeStepper(NSMakeRect(552, 60, 20, 26), 1, 24, "refreshStepped:")
        c.addSubview_(self.refreshStepper)

        c.addSubview_(_label("Clone wallpapers", NSMakeRect(12, 22, 170, 22)))
        self.cloneYes = AK.NSButton.radioButtonWithTitle_target_action_("Yes", self, "cloneClicked:")
        self.cloneYes.setFrame_(NSMakeRect(186, 22, 60, 22))
        self.cloneNo = AK.NSButton.radioButtonWithTitle_target_action_("No", self, "cloneClicked:")
        self.cloneNo.setFrame_(NSMakeRect(246, 22, 60, 22))
        c.addSubview_(self.cloneYes)
        c.addSubview_(self.cloneNo)

        c.addSubview_(_label("Keep wallpapers (days)", NSMakeRect(320, 22, 170, 22)))
        self.keepField = AK.NSTextField.alloc().initWithFrame_(NSMakeRect(490, 21, 60, 24))
        c.addSubview_(self.keepField)
        self.keepStepper = self.makeStepper(NSMakeRect(552, 20, 20, 26), 1, 3650, "keepStepped:")
        c.addSubview_(self.keepStepper)

        for f in (self.refreshField, self.keepField):
            f.setTarget_(self)
            f.setAction_("fieldEdited:")             # keep the steppers in sync with typing
            f.cell().setSendsActionOnEndEditing_(True)
            f.setAlignment_(AK.NSTextAlignmentRight)

        self.loginCheck = AK.NSButton.checkboxWithTitle_target_action_("Start potd at login", None, None)
        self.loginCheck.setFrame_(NSMakeRect(620, 62, 180, 22))
        c.addSubview_(self.loginCheck)
        self.captionCheck = AK.NSButton.checkboxWithTitle_target_action_("Photo info on wallpaper", None, None)
        self.captionCheck.setFrame_(NSMakeRect(620, 22, 190, 22))
        self.captionCheck.setToolTip_("Photos: show date, place and camera in the bottom-left corner")
        c.addSubview_(self.captionCheck)

        # ---- bottom row
        v.addSubview_(_label(f"potd {__version__}", NSMakeRect(20, 22, 200, 20), 11, secondary=True))
        v.addSubview_(_button("Save and Close", NSMakeRect(W - 268, 14, 140, 32), self, "saveAndClose:"))
        v.addSubview_(_button("Quit", NSMakeRect(W - 124, 14, 104, 32), self, "quit:"))

    @objc.python_method
    def makeStepper(self, frame, lo, hi, action):
        st = AK.NSStepper.alloc().initWithFrame_(frame)
        st.setMinValue_(lo)
        st.setMaxValue_(hi)
        st.setIncrement_(1)
        st.setValueWraps_(False)
        st.setTarget_(self)
        st.setAction_(action)
        return st

    @objc.python_method
    def makeInfoButton(self, frame):
        b = AK.NSButton.alloc().initWithFrame_(frame)
        img = None
        if hasattr(AK.NSImage, "imageWithSystemSymbolName_accessibilityDescription_"):
            img = AK.NSImage.imageWithSystemSymbolName_accessibilityDescription_("info.circle", "Info")
        if img:
            b.setImage_(img)
            b.setImagePosition_(AK.NSImageOnly)
        else:
            b.setTitle_("ⓘ")
        b.setBordered_(False)
        b.setToolTip_("Source info and API key")
        b.setTarget_(self)
        b.setAction_("infoClicked:")
        return b

    # ================================================================ info / API key window
    @objc.python_method
    def buildInfoPanel(self):
        pw, ph = 520, 250
        style = AK.NSWindowStyleMaskTitled | AK.NSWindowStyleMaskClosable
        p = InfoPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, pw, ph), style, AK.NSBackingStoreBuffered, False)
        p.setReleasedWhenClosed_(False)
        c = p.contentView()
        self.infoTitle = _label("", NSMakeRect(20, ph - 44, pw - 40, 24), 15, bold=True)
        c.addSubview_(self.infoTitle)

        c.addSubview_(_label("URL", NSMakeRect(20, ph - 80, 70, 20)))
        self.infoURL = AK.NSTextField.alloc().initWithFrame_(NSMakeRect(96, ph - 82, pw - 116, 24))
        self.infoURL.setEditable_(False)
        self.infoURL.setSelectable_(True)
        self.infoURL.setBezeled_(False)
        self.infoURL.setDrawsBackground_(False)
        c.addSubview_(self.infoURL)

        self.infoKeyLabel = _label("API key", NSMakeRect(20, ph - 116, 70, 20))
        c.addSubview_(self.infoKeyLabel)
        self.infoKey = AK.NSTextField.alloc().initWithFrame_(NSMakeRect(96, ph - 119, pw - 116, 24))
        self.infoKey.setPlaceholderString_("paste your API key here")
        self.infoKey.setDelegate_(self)          # -> controlTextDidChange_
        c.addSubview_(self.infoKey)

        self.infoNote = _label("", NSMakeRect(96, ph - 146, pw - 116, 20), 11, secondary=True)
        c.addSubview_(self.infoNote)
        self.infoResult = AK.NSTextField.wrappingLabelWithString_("")
        self.infoResult.setFrame_(NSMakeRect(20, 54, pw - 40, 40))
        self.infoResult.setFont_(AK.NSFont.systemFontOfSize_(12))
        c.addSubview_(self.infoResult)

        self.infoTest = _button("Test", NSMakeRect(pw - 214, 14, 96, 32), self, "infoTest:")
        c.addSubview_(self.infoTest)
        self.infoSave = _button("Save", NSMakeRect(pw - 112, 14, 96, 32), self, "infoSave:")
        c.addSubview_(self.infoSave)
        self.infoPanel = p
        self.infoTesting = False

    @objc.python_method
    def infoValue(self):
        """What would be saved: the key in the field, or '' for sources without a key."""
        return str(self.infoKey.stringValue()).strip() if self.infoSite in API_KEY_FIELDS else ""

    @objc.python_method
    def updateInfoButtons(self):
        busy = self.infoTesting
        if self.infoSite in API_KEY_FIELDS:
            value = self.infoValue()
            tested = self.infoApproved is not None and self.infoApproved == value
            can_save = not busy and tested                 # key passed the test, unchanged since
            can_test = not busy and not tested and value != ""
        else:
            can_save = not busy                            # fixed URL: nothing to test first
            can_test = False
        self.infoSave.setEnabled_(can_save)
        self.infoTest.setEnabled_(can_test)
        # Return presses whichever button is available.
        self.infoSave.setKeyEquivalent_("\r" if can_save else "")
        self.infoTest.setKeyEquivalent_("\r" if can_test else "")

    @objc.python_method
    def setInfoResult(self, text, color=None):
        self.infoResult.setTextColor_(color or AK.NSColor.secondaryLabelColor())
        self.infoResult.setStringValue_(text)

    def infoClicked_(self, sender):
        if getattr(self, "infoPanel", None) is None:
            self.buildInfoPanel()
        site = SITES[sender.tag()]
        self.infoSite = site
        self.infoTesting = False
        self.infoApproved = None
        info = SOURCE_INFO.get(site, {})
        self.infoPanel.setTitle_(f"{site} — source info")
        self.infoTitle.setStringValue_(site)
        self.infoURL.setStringValue_(info.get("url", ""))
        uses_key = site in API_KEY_FIELDS
        self.infoKeyLabel.setHidden_(not uses_key)
        self.infoKey.setHidden_(not uses_key)
        self.infoKey.setStringValue_((self.settings.api_key(site) or "") if uses_key else "")
        self.infoNote.setStringValue_(info.get("note", ""))
        self.setInfoResult("")
        self.updateInfoButtons()
        self.infoPanel.center()
        AK.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.infoPanel.makeKeyAndOrderFront_(None)
        if uses_key:
            self.infoPanel.makeFirstResponder_(self.infoKey)
            if self.infoValue():
                self.startInfoTest(on_open=True)       # check the saved key right away
            else:
                self.setInfoResult("Enter an API key and click Test.")

    def controlTextDidChange_(self, note):
        # Any edit of the key invalidates the last test: Test on, Save off.
        if getattr(self, "infoPanel", None) is not None and note.object() == self.infoKey:
            self.infoApproved = None
            self.setInfoResult("Changed — click Test." if self.infoValue() else "Enter an API key and click Test.")
            self.updateInfoButtons()

    def infoTest_(self, sender):
        self.startInfoTest()

    @objc.python_method
    def startInfoTest(self, on_open=False, then_save=False):
        site, value = self.infoSite, self.infoValue()
        if not site or self.infoTesting:
            return
        self.infoTesting = True
        self.setInfoResult(("Checking the saved key…" if on_open else f"Testing {site}…"))
        self.updateInfoButtons()
        keys = {site: value} if site in API_KEY_FIELDS else None
        self.runInBackground(lambda: self.engine.test_sources([site], keys),
                             lambda res, err: self.infoTestDone(site, value, res, err, on_open, then_save))

    @objc.python_method
    def infoTestDone(self, site, value, res, err, on_open=False, then_save=False):
        self.infoTesting = False
        if site != self.infoSite or not self.infoPanel.isVisible():
            self.updateInfoButtons()           # window closed or reopened for another source
            return
        ok, msg = (False, str(err)) if err else res[site][:2]
        if ok and value != self.infoValue():
            self.setInfoResult("Changed during the test — click Test again.")
        elif ok and then_save:
            self.finishInfoSave(site)
            return
        elif ok:
            self.infoApproved = value
            self.setInfoResult(("✓ The saved key works." if on_open else f"✓ Test OK: {msg}.")
                               + f" Save enables {site}.", AK.NSColor.systemGreenColor())
        else:
            self.infoApproved = None
            self.setInfoResult(f"✗ Test failed: {msg}", AK.NSColor.systemRedColor())
        self.updateInfoButtons()

    def infoSave_(self, sender):
        site = self.infoSite
        if not site or self.infoTesting:
            return
        if site in API_KEY_FIELDS:
            if self.infoApproved is None or self.infoApproved != self.infoValue():
                return                         # not tested (button is greyed out anyway)
            self.finishInfoSave(site)
        else:
            # Fixed URL: Save checks the source is reachable, then enables it.
            self.startInfoTest(then_save=True)

    @objc.python_method
    def finishInfoSave(self, site):
        self.engine.save_source(site, self.infoValue() if site in API_KEY_FIELDS else None)
        self.siteChecks[SITES.index(site)].setState_(ON)     # always enabled, even if unticked by hand
        self.infoPanel.orderOut_(None)
        self.setStatus(f"{site} saved and enabled.")

    @objc.python_method
    def showDisabledReasons(self):
        """Tell why potd unticked a source (it may have happened while the window was closed)."""
        reasons = {s: r for s, r in self.engine.state.disabled_reasons.items()
                   if not self.settings.enabled.get(s)}
        for i, site in enumerate(SITES):
            why = reasons.get(site)
            self.siteChecks[i].setToolTip_(f"Unticked by potd: {why}" if why else None)
        if reasons:
            self.setStatus("Unticked by potd — " + "; ".join(f"{s}: {r}" for s, r in reasons.items()))

    def loadSettingsIntoControls(self):
        s = self.settings
        for i, site in enumerate(SITES):
            self.siteChecks[i].setState_(ON if s.enabled.get(site) else OFF)
        h, m = s.download_hm
        cal = NSCalendar.currentCalendar()
        self.timePicker.setDateValue_(cal.dateBySettingHour_minute_second_ofDate_options_(h, m, 0, NSDate.date(), 0))
        self.refreshField.setIntegerValue_(s.refresh_hours)
        self.refreshStepper.setIntegerValue_(s.refresh_hours)
        self.keepField.setIntegerValue_(s.keep_days)
        self.keepStepper.setIntegerValue_(s.keep_days)
        self.cloneYes.setState_(ON if s.clone_wallpapers else OFF)
        self.cloneNo.setState_(OFF if s.clone_wallpapers else ON)
        self.loginCheck.setState_(ON if s.start_at_login else OFF)
        self.captionCheck.setState_(ON if s.photo_caption else OFF)
        self.updateStatus()

    # ================================================================ actions: window/menu
    def showWindow_(self, sender):
        self.setDockIcon(True)                   # window open -> icon in the Dock
        AK.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)
        self.updateStatus()
        self.showDisabledReasons()
        self.showFirstPreview()
        # Opening potd (Applications, Spotlight, `open -a potd`, or "Open potd…" in the
        # menu bar) downloads the pictures of all enabled sources. Daily sources that
        # already have today's picture make no network call.
        self.openDownloads()

    @objc.python_method
    def openDownloads(self):
        if time.time() - self.last_test_ts > 30:      # not again for a quick re-open
            self.last_test_ts = time.time()
            self.startDownloads()

    @objc.python_method
    def showFirstPreview(self, results=None):
        """Show Bing first (or the first source, in list order, that has a picture)."""
        for site in SITES:
            pic = (results or {}).get(site)
            if pic is not None and not isinstance(pic, Exception):
                self.showPreview(pic.path)
                return
            if results is None and self.settings.enabled.get(site):
                pics = self.engine.storage.pictures(site)
                if pics:
                    self.showPreview(pics[0].path)
                    return

    @objc.python_method
    def setDockIcon(self, visible):
        """Dock icon only while the window is open; the menu bar icon is always there."""
        app = AK.NSApplication.sharedApplication()
        policy = AK.NSApplicationActivationPolicyRegular if visible else AK.NSApplicationActivationPolicyAccessory
        if app.activationPolicy() != policy:
            app.setActivationPolicy_(policy)
            if visible:
                icon = AK.NSImage.imageNamed_("potd")        # the app icon (potd.icns in the bundle)
                if icon:
                    app.setApplicationIconImage_(icon)

    def windowWillClose_(self, note):
        if note.object() == self.window:
            if getattr(self, "infoPanel", None) is not None:
                self.infoPanel.orderOut_(None)
            # Red button / Cmd-W: throw away unsaved edits (after Save and Close this
            # simply shows the values that were just saved).
            self.loadSettingsIntoControls()
            self.setDockIcon(False)              # window closed -> only the menu bar icon stays

    def quit_(self, sender):
        AK.NSApplication.sharedApplication().terminate_(None)

    def openFolder_(self, sender):
        root = Path(self.settings.storage_root)
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        AK.NSWorkspace.sharedWorkspace().openURL_(NSURL.fileURLWithPath_(str(root)))

    def revealPreview_(self, sender):
        if self.preview_path:
            AK.NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_([NSURL.fileURLWithPath_(str(self.preview_path))])
        else:
            self.openFolder_(sender)

    # ================================================================ actions: sources
    def siteClicked_(self, sender):
        site = SITES[sender.tag()]
        if site in self.busy:
            return
        self.busy.add(site)
        sender.setEnabled_(False)
        sender.setTitle_(f"{site} …")
        self.setStatus(f"Loading {site}…")
        self.runInBackground(lambda: self.engine.fetch_one(site),
                             lambda pic, err: self.siteDone(site, sender, pic, err))

    @objc.python_method
    def siteDone(self, site, button, pic, err):
        self.busy.discard(site)
        button.setEnabled_(True)
        button.setTitle_(site)
        if err:
            self.setStatus(f"{site}: {err}")
            return
        self.engine.finish_fetch(pic)
        self.showPreview(pic.path)
        self.setStatus(f"{site}: {'downloaded' if pic.new else 'already downloaded'} {pic.path.name}")

    @objc.python_method
    def showPreview(self, path):
        img = AK.NSImage.alloc().initWithContentsOfFile_(str(path))
        self.imageView.setImage_(img)
        self.preview_path = Path(path)
        self.caption.setStringValue_(self.engine.caption(path))
        self.caption.setToolTip_(str(path))
        self.setNowButton.setEnabled_(img is not None)

    def setWallpaperNow_(self, sender):
        if not self.preview_path:
            return
        n = self.engine.set_now(self.preview_path)
        self.setStatus(f"Wallpaper set on {n} screen(s). Next change in {_fmt_secs(self.settings.refresh_rate)}.")

    def nextWallpaper_(self, sender):
        n = self.engine.rotate()
        if n == 0 and not (self.engine.day_pool() or self.engine.all_pool()):
            self.setStatus("No pictures yet — click a source or “Download All Now”.")
        else:
            self.updateStatus()

    def downloadNow_(self, sender):
        self.startDownloads(manual=True)

    @objc.python_method
    def startDownloads(self, manual=False, sites=None, refresh=False):
        """Download all enabled sources (or just `sites`).
        Daily sources that already have today's picture are answered from disk (no network);
        Unsplash and PicSum fetch a new picture. `refresh`: rotate the wallpaper afterwards."""
        if self.downloading:
            return False
        retry_only = sites is not None
        sites = [x for x in (sites or self.settings.enabled_sites()) if self.settings.enabled.get(x)]
        if not sites:
            if manual:
                self.setStatus("No sources enabled.")
            return False
        daily = not retry_only and self.engine.download_due()
        self.downloading = True
        self.dlButton.setEnabled_(False)
        self.setStatus(("Retrying " + ", ".join(sites) + "…") if retry_only
                       else "Refreshing…" if refresh
                       else f"Downloading pictures of the day from {len(sites)} source(s)…")
        self.runInBackground(lambda: self.engine.run_downloads(sites),
                             lambda res, err: self.downloadsDone(res, err, daily, retry_only, refresh))
        return True

    @objc.python_method
    def downloadsDone(self, results, err, daily=False, retry_only=False, refresh=False):
        self.downloading = False
        self.dlButton.setEnabled_(True)
        if err:
            self.setStatus(f"Download failed: {err}")
            if refresh:
                self.engine.rotate()
            return
        r = self.engine.finish_downloads(results, daily=daily)
        for site in r["disabled"]:
            self.siteChecks[SITES.index(site)].setState_(OFF)
        if r["disabled"]:
            self.showDisabledReasons()
        parts = [f"{len(r['pictures'])} picture(s) ready ({len(r['new'])} new)"]
        if r["retry"]:
            parts.append("retry at next refresh: " + ", ".join(
                f"{s} ({r['errors'].get(s, 'not published yet')})" for s in r["retry"]))
        if r["disabled"]:
            parts.append("disabled: " + "; ".join(f"{s} ({r['errors'][s]})" for s in r["disabled"]))
        self.setStatus(" · ".join(parts))
        if refresh:
            self.engine.rotate()                    # next wallpaper, new pictures included
        elif not retry_only:
            self.showFirstPreview(results)          # Bing first

    # ================================================================ actions: settings
    def keepStepped_(self, sender):
        self.keepField.setIntegerValue_(sender.integerValue())

    def refreshStepped_(self, sender):
        self.refreshField.setIntegerValue_(sender.integerValue())

    def fieldEdited_(self, sender):
        # Clamp what was typed and move the stepper along; nothing is saved yet.
        if sender == self.refreshField:
            v = min(24, max(1, sender.integerValue()))
            self.refreshField.setIntegerValue_(v)
            self.refreshStepper.setIntegerValue_(v)
        else:
            v = min(3650, max(1, sender.integerValue()))
            self.keepField.setIntegerValue_(v)
            self.keepStepper.setIntegerValue_(v)

    def cloneClicked_(self, sender):
        self.cloneYes.setState_(ON if sender == self.cloneYes else OFF)
        self.cloneNo.setState_(ON if sender == self.cloneNo else OFF)

    def saveAndClose_(self, sender):
        """Save everything on the main screen, apply it, close the window."""
        self.window.makeFirstResponder_(None)        # commit a field that's still being edited
        s = self.settings
        old_clone, old_keep, old_login = s.clone_wallpapers, s.keep_days, s.start_at_login
        old_enabled = dict(s.enabled)

        comps = NSCalendar.currentCalendar().components_fromDate_(
            AK.NSCalendarUnitHour | AK.NSCalendarUnitMinute, self.timePicker.dateValue())
        s.download_time = f"{comps.hour():02d}:{comps.minute():02d}"
        s.refresh_hours = self.refreshField.integerValue()
        s.keep_days = self.keepField.integerValue()
        s.clone_wallpapers = self.cloneYes.state() == ON
        s.photo_caption = self.captionCheck.state() == ON
        for i, site in enumerate(SITES):
            s.enabled[site] = self.siteChecks[i].state() == ON

        want_login = self.loginCheck.state() == ON
        if want_login != old_login:
            ok, msg = set_login_item(want_login)
            if not ok:
                self.loginCheck.setState_(ON if old_login else OFF)
                self.setStatus(f"Not saved: {msg}")
                return                               # keep the window open
            s.start_at_login = want_login

        self.engine.save_settings()                  # also normalises the values
        if s.clone_wallpapers != old_clone:
            self.engine.override = None
            self.engine.override_keys = set()
        if s.clone_wallpapers != old_clone or s.enabled != old_enabled:
            self.engine.apply(force=True)
        if s.keep_days < old_keep:
            self.runInBackground(self.engine.prune, lambda r, e: None)
        newly = [x for x in SITES if s.enabled[x] and not old_enabled.get(x)]
        for x in newly:
            self.engine.state.disabled_reasons.pop(x, None)
        if newly:
            self.engine.save_state()
        if newly:
            self.startDownloads(sites=newly)         # get today's picture of re-enabled sources
        self.setStatus("Settings saved.")
        self.window.performClose_(None)

    # ================================================================ timers & system events
    def tick_(self, _):
        try:
            if self.engine.rotation_due() and not self.downloading:
                # Every refresh: Unsplash/PicSum get a new picture, daily sources are only
                # contacted if today's picture is still missing (failed or not published yet).
                # The wallpaper changes when that's done.
                if not self.startDownloads(refresh=True):
                    self.engine.rotate()
            if not self.downloading and self.engine.download_due():
                self.startDownloads()
            self.updateStatus()
        except Exception:
            log.exception("tick failed")

    def spaceChanged_(self, note):
        # A Space we haven't painted yet (or since the last change) gets its picture now.
        self.performSelector_withObject_afterDelay_("applyNow:", None, 0.4)

    def screensChanged_(self, note):
        self.performSelector_withObject_afterDelay_("applyNow:", None, 1.0)

    def didWake_(self, note):
        self.performSelector_withObject_afterDelay_("tick:", None, 10.0)   # let Wi-Fi come back

    def applyNow_(self, _):
        try:
            self.engine.apply()
        except Exception:
            log.exception("apply failed")

    # ================================================================ helpers
    @objc.python_method
    def runInBackground(self, work, done):
        def runner():
            try:
                res, err = work(), None
            except Exception as e:
                log.exception("background task failed")
                res, err = None, e
            AppHelper.callAfter(done, res, err)
        threading.Thread(target=runner, daemon=True).start()

    @objc.python_method
    def statusText(self, short=False):
        e, st = self.engine, self.engine.state
        nxt = e.next_download_at()
        when = "today" if nxt.date() == dt.date.today() else "tomorrow"
        dl = f"next download {when} {nxt:%H:%M}"
        if self.downloading:
            dl = "downloading…"
        change = f"next change in {_fmt_secs(st.last_rotation_ts + self.settings.refresh_rate - time.time())}"
        last = f"last download {st.last_download_date}" if st.last_download_date else "no download yet"
        return f"{dl} · {change}" if short else f"{last} · {dl} · {change}"

    @objc.python_method
    def setStatus(self, text):
        self.statusLabel.setStringValue_(text)
        self.statusLabel.setToolTip_(text)
        self._status_until = time.time() + 30   # keep a message visible for a while

    @objc.python_method
    def updateStatus(self):
        if time.time() < getattr(self, "_status_until", 0):
            return
        self.statusLabel.setStringValue_(self.statusText().capitalize())


_delegate = None


def _setup_logging():
    log.setLevel(logging.INFO)
    try:
        p = Path.home() / "Library" / "Logs" / "potd.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(p, maxBytes=512 * 1024, backupCount=2)
    except OSError:
        h = logging.StreamHandler(sys.stderr)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)


def main():
    global _delegate
    _setup_logging()
    app = AK.NSApplication.sharedApplication()
    _delegate = AppDelegate.alloc().init()
    app.setDelegate_(_delegate)
    app.setActivationPolicy_(AK.NSApplicationActivationPolicyAccessory)
    AppHelper.runEventLoop()
