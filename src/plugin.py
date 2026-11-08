# -*- coding: utf-8 -*-
# HbbTV 2.0 plugin: HbbTV 2.0 test pages and applications on top of enigma2
#
# Shows a WPE WebKit page on top of enigma2 (via the external wpe_fb process) and gives it the remote control:
#
#   - a full-screen, borderless, transparent Screen is opened. As the top screen it is the only one whose
#     actions run, so the remote keys go to it and not to the infobar / channel list underneath;
#   - every key is forwarded to wpe_fb through a FIFO (/tmp/wpe_fb.ctl) as "key <name>" and arrives in the page as
#     a normal DOM keydown event;
#   - EXIT closes the app: "quit" is sent, wpe_fb releases the screen, then enigma2 is told to repaint everything
#     (getDesktop(0).paint()) so nothing of the overlay is left behind.
#
# wpe_fb itself keeps the overlay on top of enigma2's redraws (page flips) - see wpe_fb.c "guard" mode.
#
# Files it expects next to this plugin (installed with it):
#   run_wpe.sh   starts wpe_fb with the right environment
#   pages/*.html the pages offered in the chooser
import glob
import os
import re
import shlex
import socket
import threading
from sys import maxsize
from urllib.parse import urlparse

from enigma import eActionMap, eConsoleAppContainer, ePoint, eSize, eTimer, getDesktop, iPlayableService, iServiceInformation
from Components.ActionMap import ActionMap
from Components.config import ConfigSelection, ConfigText, ConfigYesNo
from Components.Sources.StaticText import StaticText
from Components.Pixmap import Pixmap
from Plugins.Plugin import PluginDescriptor
from Screens.ChoiceBox import ChoiceBox
from Components.Label import Label
from Screens.MessageBox import MessageBox
from Screens.Screen import Screen
from Screens.Setup import Setup

from . import _
from .ait import AIT_TIMEOUT, AitCollection, appName, currentChannel, locatorOf, startAitReader
from .video import VideoHandover, readComponents

try:
	from enigma import getEGLVersionString, getGLESVersionString
except ImportError:  # an enigma2 that does not report them
	def getEGLVersionString():
		return ""

	def getGLESVersionString():
		return ""

BASE = os.path.dirname(os.path.abspath(__file__))
RUN = f"{BASE}/run_wpe.sh"
PAGES = f"{BASE}/pages"
CTL = "/tmp/wpe_fb.ctl"
EVT = "/tmp/wpe_fb.evt"
LOG = "/tmp/wpe_fb.log"
RED_DELAY = 1500  # ms between "the first page has loaded" and the red key that the red button gives the application
OVERLAY_FILES = "/tmp/wpe_overlay"  # wpe_fb --overlay-png writes /tmp/wpe_overlay_<n>.png (run_wpe.sh)
AIT_FILE = "/tmp/wpe_ait.txt"  # the AIT sections of the service, "<pid> <sid> <hex>" per line, read by run_wpe.sh
CONF = "/etc/enigma2/hbbtv2.conf"  # KEY=VALUE lines, read by run_wpe.sh (see there for the defaults)
PERSIST_DIR = "/media/hdd/.wpe-data"  # HTTP cache, cookies and local storage of the applications (WPE_PERSIST_DIR in the conf overrides)
REPEAT_KEYS = ["up", "down", "left", "right"]  # keys that have a separate "<name>Repeat" action for the remote's auto-repeat

# Keys forwarded to the page; the names match the "mapto" values in keymap.xml and wpe_fb's key table.
FORWARDED_KEYS = ["up", "down", "left", "right", "ok", "back", "menu", "red", "green", "yellow", "blue",
	"play", "pause", "stop", "ff", "rew", "rec"] + [str(n) for n in range(10)]


# The keys an application running on top of the live picture (overlay mode) can take from enigma2: it asks for them by key set (a mask; wpe_fb reports
# it as the event "keyset <mask>"). enigma2 key code -> the name wpe_fb knows.
KEY_NAMES = {}
try:
	from keyids import KEYIDS
	for _id, _name in (("KEY_UP", "up"), ("KEY_DOWN", "down"), ("KEY_LEFT", "left"), ("KEY_RIGHT", "right"), ("KEY_OK", "ok"), ("KEY_BACK", "back"),
			("KEY_EXIT", "exit"), ("KEY_RED", "red"), ("KEY_GREEN", "green"), ("KEY_YELLOW", "yellow"), ("KEY_BLUE", "blue"), ("KEY_PLAY", "play"),
			("KEY_PAUSE", "pause"), ("KEY_STOP", "stop"), ("KEY_FASTFORWARD", "ff"), ("KEY_REWIND", "rew")) + tuple(("KEY_%d" % n, str(n)) for n in range(10)):
		if _id in KEYIDS:
			KEY_NAMES[KEYIDS[_id]] = _name
except ImportError:
	pass
KEYSET_KEYS = {0x1: ("red",), 0x2: ("green",), 0x4: ("yellow",), 0x8: ("blue",), 0x10: ("up", "down", "left", "right", "ok", "back"),
	0x20: ("play", "pause", "stop", "ff", "rew"), 0x100: tuple(str(n) for n in range(10))}


def claimedKeys(mask):
	"""The keys of a HbbTV key set mask. EXIT is only taken once the application is interactive (asks for the navigation keys): until then it is
	enigma2's (closing its infobar); after that it ends the application."""
	names = set()
	for bit, keys in KEYSET_KEYS.items():
		if mask & bit:
			names.update(keys)
	if mask & 0x10:
		names.add("exit")
	return names


handovers = []  # a handover has to outlive its screen: its timer finishes the job after the screen is gone


class WPEScreen(Screen):
	skin = """<screen name="WPEScreen" position="0,0" size="e,e" resolution="1920,1080" flags="wfNoBorder" backgroundColor="transparent">
		<widget name="loading" position="center,center" size="640,90" font="Regular;36" halign="center" valign="center" foregroundColor="#ffffff" backgroundColor="#c0101820" zPosition="10" />
	</screen>"""

	def __init__(self, session, page, hwvideo=False, channel=None, ait=None, pressRed=False, overlay=False):
		if overlay:
			# The skin of the automatic banner: a transparent screen with one Pixmap widget, which shows the visible part of the application's frame (a PNG
			# from wpe_fb, see showOverlay) on top of everything enigma2 has on the screen. The screen is 1x1 until there is something to show, and
			# then only as big as that (moved and resized in showOverlay): a window over the whole desktop makes enigma2 redraw everything below it, and the
			# infobar flickered when the banner started.
			self.skin = """<screen name="WPEScreen" position="0,0" size="1,1" flags="wfNoBorder" backgroundColor="transparent">
				<widget name="overlay" position="0,0" size="1,1" zPosition="5" alphatest="blend" />
			</screen>"""
		Screen.__init__(self, session)
		self.page = page
		self.hwvideo = hwvideo
		self.channel = channel  # (onid, tsid, sid, name) of the service the box is tuned to, for an application of a channel
		self.ait = ait  # the AIT of that service (AitCollection): given to the engine, which starts the autostart application / the one chosen
		self.aitSent = {}  # section number -> the section wpe_fb has been given
		self.pressRed = pressRed  # started by the red button: press it for the user once the application is there
		self.overlay = overlay  # runs on top of the live picture and enigma2's own screen (autostart): not a screen of its own, keys by key set
		self.keyset = 0  # the keys the application has asked for (event "keyset"), see claimedKeys()
		self.redTimer = eTimer()
		self.redTimer.callback.append(lambda: self.sendKey("red"))
		screens.append(self)
		self.handover = None
		self.closing = False
		self.finished = False
		self.started = False
		self.modal = False  # an overlay that has become the executing screen (goModal)

		actions = {name: (lambda name=name: self.sendKey(name)) for name in FORWARDED_KEYS}
		actions.update({name + "Repeat": (lambda name=name: self.sendKey(name, repeat=True)) for name in REPEAT_KEYS})
		actions["exit"] = self.stop
		self["actions"] = ActionMap(["HbbTV2Actions"], actions, -1)

		self.killTimer = eTimer()
		self.killTimer.callback.append(self.forceKill)

		# events from wpe_fb (see "decoder handover" there): it writes "acquire" when media needs the decoder and "release" when
		# it is done with it; polled, an enigma2 timer being the simplest way to watch a FIFO
		self.evtFd = None
		self.evtBuf = b""
		self.evtTimer = eTimer()
		self.evtTimer.callback.append(self.pollEvents)
		self.freeTimer = eTimer()
		self.freeTimer.callback.append(self.decoderFree)

		# starting takes a few seconds (wpe_fb and its web process, then the page): say so until wpe_fb reports the first page ("loaded"),
		# or after 30 s whatever happens
		self["loading"] = Label(_("Loading HbbTV ..."))
		if overlay:
			self["overlay"] = Pixmap()  # the visible part of the application's frame (showOverlay)
			self.overlaySeq = 0
			self.overlayShown = False  # the widget is on the screen
			self.overlayGeometry = None  # (x, y, w, h) of the picture on the screen; None while the screen is 1x1
			self.overlayFull = False  # the application grew too big for pictures: wpe_fb draws it into the framebuffer now
		self.loadingTimer = eTimer()
		self.loadingTimer.callback.append(self.hideLoading)

		# the streams of the service, for the application: sent to wpe_fb whenever they change (the tracks of a channel change with the
		# programme, and when the application selects one)
		self.components = ""  # what wpe_fb was last told
		self.audioTracks = {}  # audio pid -> enigma2's track number
		self.subtitleTracks = {}  # subtitle pid -> enigma2's subtitle tuple
		self.defaultAudio = None  # the audio pid in use when the application started: where "restore the default" goes back to
		self.componentsTimer = eTimer()
		self.componentsTimer.callback.append(self.pollComponents)
		self.aitTimer = eTimer()
		self.aitTimer.callback.append(self.pollAit)

		self.container = eConsoleAppContainer()
		self.container.appClosed.append(self.appClosed)

		self.onLayoutFinish.append(self.start)
		self.onClose.append(self.cleanup)

	# --- process ---------------------------------------------------------------------------------------------

	def start(self):
		if self.started:
			return  # a screen that is created as a dialog is started by its creator and again when its layout is finished: once is enough
		self.started = True
		if not os.path.exists(RUN):
			self.session.open(MessageBox, _("%s not found") % RUN, MessageBox.TYPE_ERROR)
			self.finish()
			return
		env = "WPE_OWNER=%d " % os.getpid()  # run_wpe.sh ends wpe_fb when this process is gone (enigma2 restarted or crashed): no orphans
		env += "WPE_OVERLAY=1 " if self.overlay else ""
		if self.overlay:
			self["loading"].hide()  # an application that starts by itself on every zap does not announce itself
			self["overlay"].hide()
		self.loadingTimer.start(30000, True)
		if self.hwvideo:
			# Hardware video, but the live service keeps playing (an application runs on top of the live picture) until media
			# really needs the decoder: wpe_fb then sends "acquire" (pollEvents), and "release" when it is done with it.
			self.handover = VideoHandover(self.session)
			handovers.append(self.handover)
			try:
				os.unlink(EVT)  # a stale FIFO of an earlier run: wpe_fb creates a fresh one
			except OSError:
				pass
			self.evtTimer.start(100, False)
			env += "WPE_HWVIDEO=1 "  # (added to WPE_OVERLAY=1: the automatic banner is started with hardware video too)
		if self.channel:
			# the application is started as a broadcast-related application of this service (run_wpe.sh validates it, wpe_fb reads it)
			env += 'WPE_CHANNEL="%d %d %d %s" ' % self.channel
			# its streams too, known before the application starts (run_wpe.sh sends them before "app")
			components = self.readServiceComponents()
			if components:
				env += 'WPE_COMPONENTS="%s" ' % components
			self.componentsTimer.start(1000, False)
		if self.ait and self.ait.sections and self.channel:
			# the real AIT: run_wpe.sh gives these sections to the engine (after "service", before the application is asked for); the reader goes
			# on, and what it finds later (a new version of the table) is passed on by pollAit
			try:
				with open(AIT_FILE, "w") as f:
					f.write(self.ait.text())
				self.aitSent = dict(self.ait.sections)
				env += 'WPE_AIT="%s" ' % AIT_FILE
				self.aitTimer.start(500, False)
			except OSError as error:
				print("[HbbTV2] cannot write %s: %s" % (AIT_FILE, error))
		if self.container.execute(f'{env}{RUN} {shlex.quote(self.page)} > {LOG} 2>&1') != 0:
			self.session.open(MessageBox, _("could not start wpe_fb"), MessageBox.TYPE_ERROR)
			self.finish()

	def appClosed(self, retval):
		# either we asked it to quit, or it ended by itself (crash, page load error, ...)
		self.killTimer.stop()
		self.finish()

	def forceKill(self):
		if self.container.running():
			self.container.kill()  # this ends the shell of run_wpe.sh, not wpe_fb (its child): that one is ended by name
			os.system("killall -9 wpe_fb WPEWebProcess >/dev/null 2>&1")  # (the network process ends with its parent)

	# --- control channel -------------------------------------------------------------------------------------

	def send(self, line):
		try:
			fd = os.open(CTL, os.O_WRONLY | os.O_NONBLOCK)  # fails while wpe_fb is not (yet) reading
		except OSError:
			return False
		try:
			os.write(fd, (line + "\n").encode())
		except OSError:
			return False
		finally:
			os.close(fd)
		return True

	def sendKey(self, name, repeat=False):
		if not self.closing:
			# an auto-repeat is passed on by wpe_fb at most once per delivered frame (see REPEAT_HOLD_US there)
			self.send(f"{'keyrepeat' if repeat else 'key'} {name}")

	# --- decoder handover --------------------------------------------------------------------------------------

	def pollEvents(self):
		if self.evtFd is None:
			try:
				self.evtFd = os.open(EVT, os.O_RDONLY | os.O_NONBLOCK)
			except OSError:
				return  # wpe_fb has not created it yet
		try:
			data = os.read(self.evtFd, 4096)
		except BlockingIOError:
			return
		except OSError:
			data = b""
		if not data:  # the writer has gone
			os.close(self.evtFd)
			self.evtFd = None
			return
		lines = (self.evtBuf + data).split(b"\n")
		self.evtBuf = lines.pop()  # an incomplete last line waits for the rest
		newest = None
		for line in lines:
			event = line.strip().decode(errors="replace")
			if event.startswith("overlay "):
				newest = event  # a picture that has been replaced already is not worth decoding: only the last of this batch is shown
			elif event == "acquire" and self.handover and not self.finished:
				self.handover.acquire()
				self.freeTimer.start(1200, True)  # give enigma2 a moment to close the decoder before wpe_fb hands it out
			elif event.startswith("keyset "):
				try:
					self.keyset = int(event.split()[1])
				except (ValueError, IndexError):
					pass
			elif event == "loaded":
				self.hideLoading()
				if self.pressRed:
					self.redTimer.start(RED_DELAY, True)  # the application has not registered its key handler at the moment its page is loaded
			elif event == "release" and self.handover:
				self.freeTimer.stop()
				self.handover.release()
			elif event.startswith("select "):
				self.selectComponent(event.split())
		if newest and self.overlay and not self.finished:
			self.showOverlay(newest.split())

	def showOverlay(self, words):
		"""The automatic banner: wpe_fb has saved the visible part of the application's frame ("overlay <n> <x> <y> <w> <h> <png>", screen
		coordinates; w = 0: nothing is visible) - it is shown by a Pixmap widget of this screen, so enigma2 itself composes it with everything else it
		draws: no page flips to race, nothing to restore."""
		try:
			widget = self["overlay"].instance
			if widget is None or self.overlayFull:
				return
			if words[1] == "fullscreen":
				# the application has grown too big for pictures (see png_promote in wpe_fb: enigma2 ran out of GPU memory making textures of
				# them): wpe_fb draws it into the framebuffer from now on; the picture goes away and no more come
				self.overlayFull = True
				self.hideOverlayPicture(widget)
				self.goModal()
				return
			seq, x, y, w, h = (int(word) for word in words[1:6])
			if seq <= self.overlaySeq:
				return  # an older picture than the one shown
			if w <= 0 or h <= 0:
				self.hideOverlayPicture(widget)
			else:
				# The screen is just as big as the picture and sits where it goes: enigma2 has to redraw nothing else. Only what changed is done:
				# every resize, move and show of a window costs enigma2 surfaces and textures.
				geometry = self.overlayGeometry
				if geometry is None or geometry[2:] != (w, h):
					self.instance.resize(eSize(w, h))
					widget.resize(eSize(w, h))
				if geometry is None or geometry[:2] != (x, y):
					self.instance.move(ePoint(x, y))
				widget.setPixmapFromFile(words[6])  # a file wpe_fb has already replaced by a newer one is skipped: the next event shows that one
				if not self.overlayShown:
					widget.show()
					self.overlayShown = True
				self.overlayGeometry = (x, y, w, h)
			self.overlaySeq = seq
		except (ValueError, IndexError, RuntimeError) as error:
			print("[HbbTV2] overlay: %s: %s" % (" ".join(words), error))

	def goModal(self):
		"""An application that fills the screen is a screen of its own, like the one of the red button: this dialog becomes the executing screen.
		Then enigma2's infobar is not (it shows itself on every zap and service start only while it executes): with the application drawn into the
		framebuffer, each page flip of enigma2 - and the infobar makes ten a second for five seconds - is repainted by wpe_fb's guard in a sweep that
		is seen as flicker."""
		try:
			self.session.execDialog(self)
			self.isTmp = True  # the session closes it for good (doClose: cleanup, onClose) when it is closed, as for a screen made with open()
			self.modal = True
		except Exception as error:
			print("[HbbTV2] cannot make the application modal: %s" % error)

	def hideOverlayPicture(self, widget):
		if self.overlayShown:
			widget.hide()
			self.overlayShown = False
		if self.overlayGeometry is not None:
			self.instance.resize(eSize(1, 1))
			self.overlayGeometry = None

	# --- the streams of the service --------------------------------------------------------------------------------

	def readServiceComponents(self):
		"""Read the streams of the service enigma2 is playing (remembering what is needed to select one). Returns the line for wpe_fb, or None
		while there is no service (during a decoder handover the live service is stopped: what the application was told stays)."""
		try:
			service = self.session.nav.getCurrentService()
			if not service:
				return None
			text, self.audioTracks, self.subtitleTracks, current = readComponents(service)
			if self.defaultAudio is None:
				self.defaultAudio = current
			return text
		except Exception as error:  # enigma2 versions differ in what they offer: never let this get in the way of the application
			print("[HbbTV2] reading the components failed: %s" % error)
			return None

	def pollAit(self):
		"""What the AIT reader found since: new or changed sections go to wpe_fb (the engine acts on a changed table)."""
		if self.closing or self.finished or not self.ait:
			return
		for section in self.ait.drain():
			if self.aitSent.get(section[6]) != section and self.send("ait %d %d %s" % (self.ait.pid, self.ait.channel[2], section.hex())):
				self.aitSent[section[6]] = section

	def pollComponents(self):
		if self.closing or self.finished:
			return
		text = self.readServiceComponents()
		if text is not None and text != self.components and self.send("components " + text):
			self.components = text

	def selectComponent(self, words):
		"""The application chose an audio track or subtitle ("select audio|subtitle <pid>|default|none"): enigma2 switches, then the new state
		goes to the application with the next list."""
		try:
			if len(words) != 3 or words[1] not in ("audio", "subtitle"):
				return
			kind, what = words[1], words[2]
			service = self.session.nav.getCurrentService()
			if not service:
				return
			self.readServiceComponents()  # the tracks may have changed since the last poll
			if kind == "audio":
				audio = service.audioTracks()
				pid = self.defaultAudio if what == "default" else int(what) if what.isdigit() else None  # "none": enigma2 cannot switch audio off
				if audio and pid in self.audioTracks:
					audio.selectTrack(self.audioTracks[pid])
			else:
				from Screens.InfoBar import InfoBar
				if InfoBar.instance:
					track = self.subtitleTracks.get(int(what)) if what.isdigit() else None  # "default" and "none": subtitles off
					InfoBar.instance.enableSubtitle(track)
		except Exception as error:
			print("[HbbTV2] selecting %s failed: %s" % (" ".join(words), error))
		self.pollComponents()

	def decoderFree(self):
		if self.handover and self.handover.active and not self.finished:
			self.send("decoder-free")

	def hideLoading(self):
		self.loadingTimer.stop()
		self["loading"].hide()

	def closeEvents(self):
		self.evtTimer.stop()
		self.loadingTimer.stop()
		self.redTimer.stop()
		self.freeTimer.stop()
		self.componentsTimer.stop()
		self.aitTimer.stop()
		if self.ait:
			self.ait.stop()
		if self.evtFd is not None:
			os.close(self.evtFd)
			self.evtFd = None

	# --- leaving ---------------------------------------------------------------------------------------------

	def running(self):
		return not self.closing and not self.finished and self.container.running()

	def stop(self):
		if self.closing:
			return
		self.closing = True
		if self.container.running():
			self.send("quit")
			self.killTimer.start(3000, True)  # if it does not quit by itself within 3 s, kill it
		else:
			self.finish()

	def finish(self):
		if self.finished:
			return
		self.finished = True
		# wpe_fb has released the screen by now: have enigma2 redraw everything, then drop our transparent screen
		getDesktop(0).paint()
		self.closeEvents()
		if self.handover:
			self.handover.release()  # if media still had the decoder: the live service comes back, its window is fixed later
		if self.overlay and not self.modal:
			self.session.deleteDialog(self)  # it was never opened as the top screen
		else:
			self.close()

	def cleanup(self):
		if self in screens:
			screens.remove(self)
		self.closeEvents()
		if self.overlay:
			for name in glob.glob(OVERLAY_FILES + "_*.png*"):  # wpe_fb removes them when it quits; after a crash or a kill they would stay
				try:
					os.remove(name)
				except OSError:
					pass
		self.killTimer.stop()
		if self.container.running():
			self.container.kill()


def pageChosen(session, choice, channel=None, ait=None, pressRed=False):
	if choice:
		# pages that play video on the hardware plane (hw_*.html) and real applications (URLs, which may play video whenever
		# they like) take the decoder from the live service; the other test pages leave the live picture alone
		page = choice[1]
		hwvideo = page.startswith(("http://", "https://", "ait:")) or os.path.basename(page).startswith("hw_")
		session.open(WPEScreen, page, hwvideo, channel, ait, pressRed)


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


def main(session, **kwargs):
	if not os.path.isdir(PAGES):
		session.open(MessageBox, _("%s not found") % PAGES, MessageBox.TYPE_ERROR)
		return
	pages = sorted(name for name in os.listdir(PAGES) if name.endswith(".html"))
	if not pages:
		session.open(MessageBox, _("No .html pages in %s") % PAGES, MessageBox.TYPE_ERROR)
		return
	choices = [(name[:-5], os.path.join(PAGES, name)) for name in pages]
	session.openWithCallback(lambda choice: pageChosen(session, choice), ChoiceBox, title=_("HbbTV 2.0 test page  (EXIT closes the page)"), list=choices)


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
screens = []  # the application screens that are open (WPEScreen adds itself, cleanup() removes it)


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


hooks = []  # what the session start set up, kept alive


autostart = []  # the controller of the automatic banner (one)


def sessionStart(reason, session=None, **kwargs):
	if reason == 0 and session is not None and not hooks:
		hooks.append(RedButtonHook(session))
		autostart.append(AutoStart(session))
		prefetchers.append(ChannelPrefetch(session))



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


def menuHook(menuid, **kwargs):
	"""Main menu -> Setup: the settings screen."""
	if menuid == "setup":
		return [(_("HbbTV 2.0"), settingsMenu, "hbbtv2_settings", 50)]
	return []


def redButtonApp(session, **kwargs):
	"""The red button as an Extensions-menu entry: Button Setup can bind a key to it, which works whatever enigma2's own red-button action decides
	(that one calls the plugin only when enigma2 itself knows a HbbTV URL for the service)."""
	channelApp(session, redButton=True)


def Plugins(**kwargs):
	return [PluginDescriptor(name=_("HbbTV 2.0 Test"), description=_("WPE WebKit HbbTV 2.0 experiment: test pages on top of enigma2"),
		where=PluginDescriptor.WHERE_EXTENSIONSMENU, fnc=main),
		PluginDescriptor(name=_("HbbTV 2.0: channel app"), description=_("Run the HbbTV application of the current channel (ORB engine, mock platform)"),
		where=PluginDescriptor.WHERE_EXTENSIONSMENU, fnc=channelApp),
		PluginDescriptor(name=_("HbbTV 2.0: red button"), description=_("Start the HbbTV application of the current channel as the red button does (for Button Setup)"),
		where=PluginDescriptor.WHERE_EXTENSIONSMENU, fnc=redButtonApp),
		PluginDescriptor(name=_("HbbTV 2.0"), description=_("Red button, autostart, rendering, GPU memory, timing and stored data"),
		where=PluginDescriptor.WHERE_MENU, fnc=menuHook),
		PluginDescriptor(where=PluginDescriptor.WHERE_SESSIONSTART, fnc=sessionStart)]
