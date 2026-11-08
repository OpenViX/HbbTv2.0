# -*- coding: utf-8 -*-
# The application of the tuned channel: the AIT is read, the user (or the autostart / red button) picks the application and screen.py shows it.
import re
import socket
import threading
from sys import maxsize
from urllib.parse import urlparse

from enigma import eActionMap, eTimer, iPlayableService, iServiceInformation
from Screens.ChoiceBox import ChoiceBox
from Screens.MessageBox import MessageBox

from . import _
from .ait import AIT_TIMEOUT, AitCollection, appName, currentChannel, locatorOf, startAitReader
from .screen import KEY_NAMES, REPEAT_KEYS, WPEScreen, claimedKeys, pageChosen, screens
from .settings import readSettings

try:
	from enigma import getEGLVersionString, getGLESVersionString
except ImportError:  # an enigma2 that does not report them
	def getEGLVersionString():
		return ""

	def getGLESVersionString():
		return ""

MIN_EGL = (1, 4)  # what WPE WebKit needs of the driver
MIN_GLES = (2, 0)


def versionOf(text, pattern):
	m = re.search(pattern, text or "")
	return (int(m.group(1)), int(m.group(2))) if m else None


def glSupport():
	"""(True, "") when enigma2 reports an EGL and an OpenGL ES version that WPE WebKit can use, else (False, a message for the user).
	enigma2 reports what its own EGL renderer got from the driver (getEGLVersionString / getGLESVersionString): nothing when it does not run on EGL."""
	egl, gles = getEGLVersionString().strip(), getGLESVersionString().strip()
	eglVersion = versionOf(egl, r"^(\d+)\.(\d+)")
	glesVersion = versionOf(gles, r"OpenGL ES(?:-\w+)? (\d+)\.(\d+)")
	if eglVersion and glesVersion and eglVersion >= MIN_EGL and glesVersion >= MIN_GLES:
		return True, ""
	return False, _("HbbTV is not supported on this system.\n\nIt needs EGL %d.%d and OpenGL ES %d.%d or newer.\nenigma2 reports: EGL %s, OpenGL ES %s.") % (
		MIN_EGL + MIN_GLES + (egl or "none", gles.split(' "', 1)[0] or "none"))


def currentServiceAppUrl(session):
	# enigma2 keeps the autostart application URL of the AIT of the current service (see eDVBServicePMTHandler::AITready)
	service = session.nav.getCurrentService()
	info = service and service.info()
	return (info and info.getInfoString(iServiceInformation.sHBBTVUrl)) or ""


class ChannelAppLauncher:
	"""The "channel app" of the tuned service with its real AIT: read the AIT (a moment, in the background), let the user pick when several
	applications are signalled, start it. Without a readable AIT the URL enigma2 keeps for the autostart application is used, as before."""

	def __init__(self, session, redButton=False):
		self.session = session
		self.redButton = redButton  # started by the red button: the AUTOSTART application, and the red key goes to it as soon as it is there
		self.ait = None
		self.waited = 0.0
		self.box = None
		self.timer = eTimer()
		self.timer.callback.append(self.poll)
		self.afterBox = eTimer()
		self.afterBox.callback.append(self.chosen)

	def start(self):
		self.ait = takePrefetched(self.session)  # read (and parsed) since the channel started, when that has been going on
		if self.ait is not None and self.ait.failed:  # a prefetch that found nothing is read again (the red button only fires on a channel that signals an application, so the failure is transient; without the AIT the application starts as a plain URL and its dvb://current.ait links fail with "No AIT")
			self.ait.stop()
			self.ait = None
		if self.ait is None:
			try:
				started = startAitReader(self.session)
			except Exception as error:
				print("[HbbTV2] cannot read the AIT: %s" % error)
				started = None
			if not started:
				self.fallback()
				return
			self.ait = AitCollection(*started)
		self.box = None
		self.seen = -1  # section count at the last poll
		self.stable = 0.0  # seconds without a new section
		self.ait.drain()
		if self.ready():  # the prefetch has what is needed: no waiting
			self.afterBox.start(0, True)
		else:
			self.timer.start(200, False)

	def closeBox(self):
		if self.box:
			try:
				self.box.close()
			except Exception:
				pass
			self.box = None

	def ready(self):
		"""Can the choice be made now? The red button needs only the autostart application (the rest of the table follows by itself, the engine gets
		every section); the Extensions menu offers the list of all applications, so it needs the table complete and no longer changing."""
		if self.ait.failed:
			return True
		if self.redButton:
			return any(app["control"] == 1 and app["http"] for app in self.ait.apps())
		return self.ait.complete() and self.stable >= 1.0

	def poll(self):
		self.ait.drain()
		self.waited += 0.2
		if self.ait.changes != self.seen:
			self.seen, self.stable = self.ait.changes, 0.0
		else:
			self.stable += 0.2
		ready = self.ready()
		if self.box is None and not self.redButton and not ready and self.waited >= 0.6:  # (the red button starts the application without a message)
			try:
				self.box = self.session.open(MessageBox, _("Loading the list of HbbTV applications..."), MessageBox.TYPE_INFO, enable_input=False)
			except Exception as error:
				print("[HbbTV2] cannot show the loading message: %s" % error)
		if ready or self.ait.failed or self.waited >= AIT_TIMEOUT + 1:
			self.timer.stop()
			# A modal screen that is closed only leaves when the session has run its delay timer; until then nothing is executing and opening the next
			# one raises "Modal open are allowed only from a screen which is modal!" - and enigma2 is left with no screen to take keys (it was killed).
			# So the next screen is opened a moment after the box has been closed, not in the same call.
			had = self.box is not None
			self.closeBox()
			self.afterBox.start(400 if had else 0, True)

	def usableApps(self):
		return [app for app in self.ait.apps() if app["control"] in (1, 2) and app["http"]]  # AUTOSTART or PRESENT, over HTTP (not a broadcast carousel)

	def chosen(self):
		apps = self.usableApps() if self.ait.sections else []
		if not apps:
			self.ait.stop()
			self.fallback()
			return
		autostart = [app for app in apps if app["control"] == 1]
		print("[HbbTV2] AIT: %d application(s), %d autostart, %s" % (len(apps), len(autostart), "red button" if self.redButton else "extensions menu"))
		# the red button takes the autostart application; the Extensions menu always offers the list (even of one application), so the user sees what the channel signals
		if self.redButton and (len(apps) == 1 or autostart):
			self.launch(autostart[0] if autostart else apps[0])
			return
		entries = [("%s%s" % (appName(app), "  (autostart)" if app["control"] == 1 else ""), app) for app in sorted(apps, key=lambda a: a["control"] != 1)]
		self.session.openWithCallback(self.picked, ChoiceBox, title=_("HbbTV applications of %s  (EXIT closes)") % self.ait.channel[3], list=entries)

	def picked(self, choice):
		if choice:
			self.launch(choice[1])
		else:
			self.ait.stop()

	def launch(self, app):
		# the autostart application starts by itself (the engine runs it when the AIT is complete); another one by its locator
		spec = "" if app["control"] == 1 else locatorOf(app)
		pageChosen(self.session, (appName(app), "ait:" + spec), self.ait.channel, self.ait, pressRed=self.redButton)

	def fallback(self):
		url = currentServiceAppUrl(self.session)
		if not url.startswith(("http://", "https://")):
			self.session.open(MessageBox, _("The current channel does not signal an HbbTV application (no AIT)."), MessageBox.TYPE_INFO, timeout=5)
			return
		pageChosen(self.session, (url, url), currentChannel(self.session), pressRed=self.redButton)


launchers = []  # a launcher has to stay alive while it waits for the AIT


def channelApp(session, redButton=False, **kwargs):
	if screens or any(controller.overlay for controller in autostart):  # an application is on the screen already
		return
	supported, reason = glSupport()
	if not supported:
		session.open(MessageBox, reason, MessageBox.TYPE_ERROR, timeout=10)
		return
	launcher = ChannelAppLauncher(session, redButton)
	launchers[:] = [launcher]
	launcher.start()


class ChannelPrefetch:
	"""Starts reading the AIT of a service as soon as enigma2 starts it (evStart), so that the red button (or the automatic banner) finds the
	applications already read and parsed instead of starting from nothing. The slow parts - waiting for the PMT/AIT sections, parsing them, resolving the
	host name of the autostart application - run in a thread; evEnd (the service stops) or another evStart aborts it. takePrefetched() hands the result
	over, after which the caller owns (and stops) the collection."""

	TRIES = 20  # x 500 ms for enigma2 to know the PMT PID of the new service

	def __init__(self, session):
		self.session = session
		self.collection = None
		self.cancelled = None  # threading.Event of the running thread
		self.tries = 0
		self.timer = eTimer()
		self.timer.callback.append(self.tick)
		session.nav.event.append(self.navEvent)

	def wanted(self):
		settings = readSettings()
		return (settings["WPE_REDBUTTON"] == "1" or settings["WPE_AUTOSTART"] == "1") and glSupport()[0] and not screens and not any(c.overlay for c in autostart)

	def navEvent(self, event):
		if event in (iPlayableService.evStart, iPlayableService.evEnd):
			self.abort()
		if event == iPlayableService.evStart and self.wanted():
			self.tries = 0
			self.timer.start(500, False)

	def abort(self):
		self.timer.stop()
		if self.cancelled:
			self.cancelled.set()
		if self.collection:
			self.collection.stop()
		self.collection = self.cancelled = None

	def tick(self):
		self.tries += 1
		try:
			started = startAitReader(self.session)
		except Exception as error:
			print("[HbbTV2] prefetch: cannot read the AIT: %s" % error)
			started = None
		if started:
			self.timer.stop()
			self.collection = AitCollection(*started)
			self.cancelled = threading.Event()
			threading.Thread(target=self.work, args=(self.collection, self.cancelled), name="HbbTV2-prefetch", daemon=True).start()
		elif self.tries >= self.TRIES:
			self.timer.stop()

	@staticmethod
	def work(collection, cancelled):
		"""The thread: collect the sections until the table is complete, parse it, resolve the host of the autostart application."""
		while not cancelled.is_set():
			collection.drain()
			if collection.failed:
				return
			if collection.complete():
				break
			cancelled.wait(0.1)
		else:
			return
		for app in collection.apps():  # (parsed once, kept by the collection)
			if cancelled.is_set():
				return
			if app["control"] == 1 and app["http"]:
				host = urlparse(app["http"][0]).hostname
				if host:
					try:
						socket.getaddrinfo(host, None)  # the resolver caches the answer: the page's first request does not wait for it
					except OSError:
						pass
				return

	def take(self):
		"""The collection of the service that is playing now, or None. The thread is stopped; the caller drains and stops the collection from here on."""
		collection, cancelled = self.collection, self.cancelled
		self.collection = self.cancelled = None
		if collection is None:
			return None
		cancelled.set()
		channel = currentChannel(self.session)
		if not channel or tuple(channel[:3]) != tuple(collection.channel[:3]):
			collection.stop()
			return None
		return collection


prefetchers = []  # the prefetch of the session (one)


def takePrefetched(session):
	return prefetchers[0].take() if prefetchers else None


class AutoStart:
	"""The automatic HbbTV banner: when the box settles on a service whose AIT has an AUTOSTART application, it runs on top of the live picture (wpe_fb
	--overlay-png: the visible part of its frame is shown by a Pixmap widget of a screen on top of enigma2's own), and the keys it has asked for - and
	only those - go to it instead of to enigma2. Off unless the setting WPE_AUTOSTART is on."""

	SETTLE = 3000  # ms on one service before anything is started (zapping through channels must not start and stop a browser each time)

	def __init__(self, session):
		self.session = session
		self.overlay = None
		self.collection = None
		self.consumed = set()  # rc keys whose "make" was taken: their "break" is ours too
		self.waited = 0.0
		self.settleTimer = eTimer()
		self.settleTimer.callback.append(self.settled)
		self.aitTimer = eTimer()
		self.aitTimer.callback.append(self.pollAit)
		session.nav.event.append(self.navEvent)
		eActionMap.getInstance().bindAction("", -maxsize - 1, self.keyEvent)

	def enabled(self):
		return readSettings()["WPE_AUTOSTART"] == "1"

	def navEvent(self, event):
		if event == iPlayableService.evStart:
			self.cancelReading()
			self.settleTimer.start(self.SETTLE, True)

	def cancelReading(self):
		self.aitTimer.stop()
		if self.collection and not (self.overlay and self.overlay.ait is self.collection):
			self.collection.stop()
		self.collection = None

	def settled(self):
		channel = currentChannel(self.session)
		if self.overlay:
			if channel is None:
				return  # the service is stopped for the decoder (the application plays media): nothing changed
			if self.overlay.channel and tuple(channel[:3]) == tuple(self.overlay.channel[:3]):
				return  # the same service (the decoder came back)
			self.overlay.stop()  # another service: this application is not for it
			self.settleTimer.start(2000, True)  # start for the new service when this one has gone
			return
		if channel is None or not self.enabled() or screens or self.collection:
			return
		if not glSupport()[0]:
			return
		collection = takePrefetched(self.session)
		if collection is None:
			try:
				started = startAitReader(self.session)
			except Exception as error:
				print("[HbbTV2] autostart: cannot read the AIT: %s" % error)
				return
			collection = AitCollection(*started) if started else None
		if collection:
			self.collection = collection
			self.waited = 0.0
			self.aitTimer.start(300, False)

	def pollAit(self):
		collection = self.collection
		if not collection:
			self.aitTimer.stop()
			return
		collection.drain()
		self.waited += 0.3
		if not (collection.complete() or collection.failed or self.waited > AIT_TIMEOUT + 1):
			return
		self.aitTimer.stop()
		usable = [app for app in collection.apps() if app["control"] == 1 and app["http"]] if collection.sections else []
		if usable and self.enabled() and not screens and not self.overlay:
			self.startOverlay(collection)
		else:
			collection.stop()
			self.collection = None

	def startOverlay(self, collection):
		try:
			overlay = self.session.instantiateDialog(WPEScreen, "ait:", True, collection.channel, collection, False, True)
		except Exception as error:
			print("[HbbTV2] autostart: cannot create the overlay: %s" % error)
			collection.stop()
			self.collection = None
			return
		self.overlay = overlay
		overlay.onClose.append(self.overlayClosed)
		overlay.show()  # a dialog made with instantiateDialog() is not on the screen until it is shown (its window is 1x1 until there is a picture)
		overlay.start()

	def overlayClosed(self):
		self.overlay = None
		self.collection = None
		self.consumed.clear()

	def keyEvent(self, key, flag):
		"""enigma2's key hook: 1 = the key is taken (the application asked for it), 0 = enigma2 gets it as usual."""
		overlay = self.overlay
		if overlay is None or not overlay.running():
			return 0
		if flag == 1:  # break: goes where the make went
			if key in self.consumed:
				self.consumed.discard(key)
				return 1
			return 0
		name = KEY_NAMES.get(key)
		if name is None or flag not in (0, 2) or name not in claimedKeys(overlay.keyset):
			return 0
		self.consumed.add(key)
		if flag == 0:
			overlay.sendKey(name)
		elif name in REPEAT_KEYS:
			overlay.sendKey(name, repeat=True)
		return 1


class RedButtonHook:
	"""enigma2 calls InfoBar.onHBBTVActivation when the red button is pressed on a service that signals a HbbTV application (the hook the old
	HbbTV plugins used). The InfoBar does not exist yet when the session starts: register as soon as it does - and keep checking, slowly: another
	plugin or a GUI reload that replaces the InfoBar or its list of hooks would otherwise leave the red button dead until enigma2 is restarted."""

	def __init__(self, session):
		self.session = session
		self.reported = False
		self.timer = eTimer()
		self.timer.callback.append(self.register)
		self.timer.start(2000, False)

	def register(self):
		try:
			from Screens.InfoBar import InfoBar
			bar = InfoBar.instance
			if bar is not None:
				if self.activated not in bar.onHBBTVActivation:
					bar.onHBBTVActivation.append(self.activated)
				self.timer.start(10000, False)  # registered (or still so): from now on only a check now and then
		except Exception as error:
			if not self.reported:  # not every 2 s
				self.reported = True
				print("[HbbTV2] red button hook: %s" % error)

	def activated(self):
		if readSettings()["WPE_REDBUTTON"] == "1":  # not reached while a running automatic application has taken the red key
			channelApp(self.session, redButton=True)


autostart = []  # the controller of the automatic banner (one)
