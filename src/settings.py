# The settings of the plugin: the screen, and /etc/enigma2/hbbtv2.conf, which run_wpe.sh reads.
from Components.ActionMap import ActionMap
from Components.config import ConfigSelection, ConfigText, ConfigYesNo
from Components.Sources.StaticText import StaticText
from Screens.MessageBox import MessageBox
from Screens.Setup import Setup

from . import _

CONF = "/etc/enigma2/hbbtv2.conf"  # KEY=VALUE lines, read by run_wpe.sh (see there for the defaults)
PERSIST_DIR = "/media/hdd/.wpe-data"  # HTTP cache, cookies and local storage of the applications (WPE_PERSIST_DIR in the conf overrides)


# --- settings ------------------------------------------------------------------------------------------------
# One row per flag of /etc/enigma2/hbbtv2.conf that run_wpe.sh reads:
#   (key, title, [(value, text), ...] or None for a text field, default, help)
# "default" is what run_wpe.sh / wpe_fb use when the key is absent from the conf: a value equal to it is not written, so the conf only holds what
# was changed (and "Reset to defaults" empties it). An option with the value "" stands for "not set: leave it to WebKit / the driver".
# Settings that are not in a group below are kept in the conf untouched.
ON_OFF = [("0", _("off")), ("1", _("on"))]


def msOptions(*values):
	return [(str(value), _("%d ms") % value if value else _("off")) for value in values]


SETTING_GROUPS = [
	("Application", [
		("WPE_REDBUTTON", _("Red button starts the application"), [("1", _("on")), ("0", _("off"))], "1",
			_("The red button on a channel with an HbbTV application starts it (and gives it the red key).")),
		("WPE_AUTOSTART", _("Automatic banner when zapping"), ON_OFF, "0",
			_("Start the AUTOSTART application of a channel by itself, on top of the live picture, a few seconds after zapping.")),
		("WPE_PERSIST", _("Keep cache, cookies and storage"), [("1", _("on")), ("0", _("off"))], "1",
			_("The HTTP cache (up to 75 MB), cookies and local storage of the applications survive between runs. Without a disk or USB stick they are kept in /tmp/wpe-data and lost at reboot.")),
		("WPE_PERSIST_DIR", _("Storage directory"), None, "",
			_("Where they live. Empty = /media/hdd/.wpe-data, or /tmp/wpe-data when there is less than 100 MB free there. An absolute path of letters, digits and _ . / -")),
		("WPE_TOLERANT", _("Tolerant mode"), ON_OFF, "1",
			_("Ignore TLS certificate errors, show no WebKit error page for a failed load, and reload a crashed page instead of quitting.")),
		("WPE_UA", _("User agent"), None, "",
			_("Text added to the user agent after the HbbTV token, for a broadcaster that only serves listed TV models (Mediaset: LGE; HbbTV-Box; LaTivu_2.1.0_2026). It is put in parentheses after the HbbTV token (WebKit refuses a semicolon outside them). Starting with Mozilla/ it replaces the whole user agent, which must then be valid itself and keep an HbbTV/x.y (...;vendor;model;...) token. Empty = unchanged.")),
		("WPE_DEBUG", _("Debug log (slower)"), ON_OFF, "0",
			_("The page's console, media events, every network request and GStreamer output go to /tmp/wpe_fb.log.")),
	]),
	("Rendering", [
		("WPE_HIDPI", _("Render quality"), [("1.5", _("sharp (1.5x)")), ("1.25", _("balanced (1.25x)")), ("1.0", _("fast (1.0x)"))], "1.0",
			_("The read-back of a frame costs time in proportion to its pixels: 1.0 reacts about three times quicker than 1.5.")),
		("WPE_FAST_ANIM", _("Shorter animations"), [("1", _("on")), ("0", _("off"))], "1",
			_("CSS transitions and animations last 1 ms and there is no smooth scrolling: fewer frames per key press.")),
		("WPE_SKIA_CPU", _("Page painting"), [("1", _("CPU")), ("0", _("GPU"))], "0",
			_("CPU painting uses little GPU memory (no black tiles) and is only slightly slower. GPU painting keeps tiles as GPU textures.")),
		("WPE_HW_ACCEL", _("Software compositing"), [("", _("off")), ("never", _("on"))], "",
			_("Composite the page in software: no GPU memory is used for page layers.")),
	]),
	("GPU memory", [
		("WPE_COVER_AREA", _("Tile cover area"), [("1", _("1 (visible area only)")), ("1.5", _("1.5")), ("2", _("2 (WebKit default)")), ("3", _("3")), ("4", _("4"))], "2",
			_("Area kept in tiles as a multiple of the visible area, squared (WebKit's own 2 keeps four times the visible area). Lower = less GPU memory.")),
		("WPE_SKIA_CACHE_MB", _("Skia GPU cache"), [(str(v), _("%d MB") % v) for v in (8, 16, 32, 64, 128, 256)], "32",
			_("Limit of the Skia GPU resource cache.")),
		("WPE_TEXTURE_POOL_MB", _("Unused tile texture pool"), [(str(v), _("%d MB") % v) for v in (16, 32, 64, 128, 256)], "16",
			_("Above this size unused tile textures are released after 50 ms instead of 3 s.")),
		("WPE_TILE_SIZE", _("Tile size (GPU painting)"), [("", _("default")), ("256x256", _("256 x 256")), ("512x256", _("512 x 256")), ("512x512", _("512 x 512")), ("1024x256", _("1024 x 256"))], "",
			_("The GPU default is as wide as the viewport; smaller tiles allocate less memory per update.")),
		("WPE_SKIA_DDL", _("Deferred display lists (GPU painting)"), [("1", _("on")), ("0", _("off"))], "1",
			_("Off: paint with Skia GPU worker threads into pooled textures instead (a different memory use).")),
		("WPE_SKIA_GPU_THREADS", _("GPU painting threads"), [("", _("default"))] + [(str(v), str(v)) for v in (1, 2, 3, 4)], "",
			_("Painting threads when deferred display lists are off.")),
		("WEBKIT_DISABLE_GL_FENCE", _("GL fence sync"), [("", _("automatic")), ("0", _("use fences")), ("1", _("disable fences"))], "",
			_("Automatic: fences on Nexus boxes, off on others. Disable them when the driver crashes in glFlush() or has no fence sync.")),
	]),
	("Output and keys", [
		("WPE_LIMIT_REPEAT", _("Limit key repeat"), [("1", _("on")), ("0", _("off"))], "0",
			_("Pass the remote's auto-repeat on at most once per delivered frame: a held key moves fewer steps (a matter of feel, not speed).")),
		("WPE_NICE", _("Process priority (nice)"), [(str(v), str(v)) for v in (-20, -10, -5, 0, 5, 10)], "-20",
			_("Scheduling priority of wpe_fb and its web processes: -20 highest, 19 lowest.")),
		("WPE_EGL", _("Output path"), [("", _("automatic")), ("1", _("enigma2 EGL renderer")), ("0", _("framebuffer"))], "",
			_("Automatic: enigma2's EGL renderer when it listens for the page (Nexus boxes), else the framebuffer.")),
	]),
	("Frame timing (EGL output)", [
		("WPE_EGL_SETTLE_MS", _("Settle time"), msOptions(0, 30, 60, 100, 150, 250), "0",
			_("After a change, wait this long for the page to stop changing before showing the frame.")),
		("WPE_EGL_MAX_DELAY_MS", _("Longest delay"), msOptions(0, 100, 250, 500, 1000), "0",
			_("A frame is never held back longer than this by the settle time.")),
		("WPE_EGL_KEY_MS", _("Key window"), msOptions(0, 200, 400, 800), "400",
			_("Frames this soon after a key press are the page's answer to it and are shown at once.")),
		("WPE_EGL_HOLD_MS", _("Hold solid areas"), msOptions(0, 50, 100, 200, 400), "0",
			_("Delay solid areas of a frame to hide a flash (can show as a late flash after an animation).")),
		("WPE_EGL_HOLD_CLEAR", _("Hold cleared areas"), ON_OFF, "0", _("Apply the hold to cleared (transparent) areas too.")),
		("WPE_EGL_DIP_MS", _("Dip time"), msOptions(0, 150, 300, 600), "300", _("Duration of the picture dip copy (see wpe_fb dip_copy).")),
		("WPE_EGL_RESYNC_MS", _("Resync time"), msOptions(0, 100, 250, 500), "0",
			_("Re-read released buffers after this time to repair rows. Off by default: it can bring back older pixels.")),
		("WPE_EGL_PIG_FIRST_MS", _("Picture-in-graphics: first"), msOptions(1000, 2000, 3000, 5000), "3000",
			_("Wait before the first video rectangle appears in graphics (a loading animation must not flash the picture).")),
		("WPE_EGL_PIG_SETTLE_MS", _("Picture-in-graphics: change"), msOptions(100, 300, 500, 1000), "300",
			_("Wait before a showing video rectangle moves or goes away.")),
		("WPE_EGL_NO_HOLE", _("No transparent hole"), ON_OFF, "0", _("Do not cut a transparent hole for the video in the frame.")),
		("WPE_EGL_NO_TRIM", _("No row trimming"), ON_OFF, "0", _("Copy whole damage rectangles instead of trimming unchanged rows.")),
		("WPE_EGL_DEBUG_RECTS", _("Show damage rectangles"), ON_OFF, "0", _("Draw the copied rectangles into the frame (debugging).")),
		("WPE_NO_NEXUS_JOIN", _("Do not join Nexus"), ON_OFF, "0", _("Skip the NxClient join (debugging; the page cannot get an EGL display on Nexus boxes without it).")),
	]),
]

class Setting:
	"""A row of the table and its config element(s): a text field, an on/off switch, a time that can be switched off (a switch, and the time), or a choice."""

	def __init__(self, key, title, options, default, help):
		self.key, self.title, self.default, self.help = key, title, default, help
		off, on = _("off"), _("on")  # (the texts as the table has them, translated or not)
		self.text = options is None
		self.switch = self.on = None  # self.switch: (value for off, value for on) of an on/off (yes/no) setting, shown as enigma2's graphical switch (ConfigYesNo)
		if options is not None and len(options) == 2:
			texts = dict((text, value) for value, text in options)
			if set(texts) == {off, on}:
				self.switch = texts[off], texts[on]
		if self.text:
			self.element = ConfigText(default=default, fixed_size=False, visible_width=40)
		elif self.switch:
			self.element = ConfigYesNo(default=default == self.switch[1])
		elif ("0", off) in options:  # a choice "off" (value 0) among others: self.on is the switch in front of the time
			choices = [option for option in options if option != ("0", off)]
			self.element = ConfigSelection(choices=choices, default=default if default != "0" else choices[0][0])
			self.on = ConfigYesNo(default=default != "0")
		else:
			self.element = ConfigSelection(choices=[(option or "-", text) for option, text in options], default=default or "-")  # (the empty option as "-": a key of "" is not safe in every enigma2)

	def load(self, value):
		"""Put a conf value into the config element(s)."""
		try:
			if self.switch:
				self.element.value = value == self.switch[1]
			elif self.on:
				self.on.value = value != "0"
				if value != "0":
					self.element.value = value
			else:
				self.element.value = value if self.text else value or "-"
		except Exception:
			pass  # a value this setting does not offer: left as it was

	def reset(self):
		self.element.value = self.element.default
		if self.on:
			self.on.value = self.on.default

	def save(self):
		self.element.save()
		if self.on:
			self.on.save()

	def value(self):
		"""The value for the conf file ("" = not set)."""
		value = self.element.value
		if self.switch:
			return self.switch[1 if value else 0]
		if self.on:
			return value if self.on.value else "0"
		return "" if value == "-" else value.strip()


GROUPS = [[Setting(*row) for row in rows] for group, rows in SETTING_GROUPS]
SETTINGS = [setting for group in GROUPS for setting in group]


def loadConf():
	"""Show the values of the conf file in the settings (a key it does not have is at its default), as the saved state: nothing is "changed" until the user
	changes it. Without a conf file they stay at their defaults."""
	try:
		with open(CONF) as f:
			conf = dict(line.strip().partition("=")[::2] for line in f if "=" in line)
	except OSError:
		return
	for setting in SETTINGS:
		if setting.key in conf:
			setting.load(conf[setting.key])
		else:
			setting.reset()
		setting.save()


loadConf()


def readSettings():
	"""{key: value} of every setting, as run_wpe.sh gets it ("" = not set)."""
	return {setting.key: setting.value() for setting in SETTINGS}


def persistDir():
	value = readSettings()["WPE_PERSIST_DIR"]
	return value if value.startswith("/") and len(value) > 1 else PERSIST_DIR


def writeSettings():
	"""Write /etc/enigma2/hbbtv2.conf, which run_wpe.sh reads, from the config. Only what differs from the default is written. Keys this plugin does
	not know (e.g. WEBKIT_* set by hand) are kept as they are."""
	known = {setting.key for setting in SETTINGS}
	extra = []
	try:
		with open(CONF) as f:
			extra = [line.rstrip("\r\n") for line in f if line.partition("=")[0] not in known and line.strip()]
	except OSError:
		pass
	try:
		with open(CONF, "w") as f:
			f.write("".join(f"{setting.key}={value}\n" for setting in SETTINGS if (value := setting.value()) != setting.default and value != ""))
			f.write("".join(line + "\n" for line in extra))
		return True
	except OSError:
		return False


class WPESettings(Setup):
	"""The settings, a standard enigma2 Setup screen (like the plugins' own): LEFT/RIGHT change a value, OK edits a text, GREEN saves, RED/EXIT cancel
	(asking only when something was changed), YELLOW resets to the defaults, BLUE deletes the stored data. The values are applied at the next start of
	an application."""

	def __init__(self, session):
		loadConf()  # (the conf file may have been edited since the plugin was loaded)
		Setup.__init__(self, session, None)
		self.title = _("HbbTV 2.0 settings")
		self["key_yellow"] = StaticText(_("Restore defaults"))
		self["key_blue"] = StaticText(_("Delete stored data"))
		self["wpeActions"] = ActionMap(["ColorActions"], {"yellow": self.defaults, "blue": self.clear}, -1)
		self.addSaveNotifier(self.saved)

	def createSetup(self):
		configlist = []
		for number, group in enumerate(GROUPS):
			if number:
				configlist.append(("---",))
			for setting in group:
				if setting.on:
					configlist.append((setting.title, setting.on, setting.help))
					if setting.on.value:
						configlist.append(("  " + _("Time"), setting.element, setting.help))
				else:
					configlist.append((setting.title, setting.element, setting.help))
		self["config"].list = configlist

	def saved(self):
		if not writeSettings():
			self.session.open(MessageBox, _("Could not write %s") % CONF, MessageBox.TYPE_ERROR)

	def defaults(self):
		self.session.openWithCallback(self.defaultsConfirmed, MessageBox, _("Set every setting to its default?"), MessageBox.TYPE_YESNO, default=False)

	def defaultsConfirmed(self, yes):
		if yes:
			for setting in SETTINGS:
				setting.reset()
			self.createSetup()

	def clear(self):
		self.session.openWithCallback(lambda yes: clearStoredData(self.session) if yes else None, MessageBox,
			_("Delete the stored cache, cookies and local storage of the HbbTV applications (%s)?") % persistDir(), MessageBox.TYPE_YESNO, default=False)


def settingsMenu(session, **kwargs):
	session.open(WPESettings)


def clearStoredData(session):
	import shutil
	directory = persistDir()
	try:
		shutil.rmtree(directory)
		session.open(MessageBox, _("Stored data deleted."), MessageBox.TYPE_INFO, timeout=4)
	except FileNotFoundError:
		session.open(MessageBox, _("There is nothing stored."), MessageBox.TYPE_INFO, timeout=4)
	except OSError as e:
		session.open(MessageBox, _("Could not delete %s: %s") % (directory, e), MessageBox.TYPE_ERROR)
