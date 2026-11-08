# -*- coding: utf-8 -*-
# The screen that shows a WPE WebKit page on top of enigma2 (via the external wpe_fb process) and gives it the remote control:
#
#   - a full-screen, borderless, transparent Screen is opened. As the top screen it is the only one whose
#     actions run, so the remote keys go to it and not to the infobar / channel list underneath;
#   - every key is forwarded to wpe_fb through a FIFO (/tmp/wpe_fb.ctl) as "key <name>" and arrives in the page as
#     a normal DOM keydown event;
#   - EXIT closes the app: "quit" is sent, wpe_fb releases the screen, then enigma2 is told to repaint everything
#     (getDesktop(0).paint()) so nothing of the overlay is left behind.
#
# wpe_fb itself keeps the overlay on top of enigma2's redraws (page flips) - see wpe_fb.c "guard" mode.

import glob
import os
import shlex

from enigma import eConsoleAppContainer, ePoint, eSize, eTimer, getDesktop
from Components.ActionMap import ActionMap
from Components.Label import Label
from Components.Pixmap import Pixmap
from Screens.MessageBox import MessageBox
from Screens.Screen import Screen

from . import _
from .video import VideoHandover, readComponents

screens = []  # the application screens that are open (WPEScreen adds itself, cleanup() removes it)
BASE = os.path.dirname(os.path.abspath(__file__))
RUN = f"{BASE}/run_wpe.sh"
PAGES = f"{BASE}/pages"
CTL = "/tmp/wpe_fb.ctl"
EVT = "/tmp/wpe_fb.evt"
LOG = "/tmp/wpe_fb.log"
RED_DELAY = 1500  # ms between "the first page has loaded" and the red key that the red button gives the application
OVERLAY_FILES = "/tmp/wpe_overlay"  # wpe_fb --overlay-png writes /tmp/wpe_overlay_<n>.png (run_wpe.sh)
AIT_FILE = "/tmp/wpe_ait.txt"  # the AIT sections of the service, "<pid> <sid> <hex>" per line, read by run_wpe.sh
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


def timer(callback):
	t = eTimer()
	t.callback.append(callback)
	return t


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
		self.title = _("HbbTV 2.0")  # for summary screen
		self.page = page
		self.hwvideo = hwvideo
		self.channel = channel  # (onid, tsid, sid, name) of the service the box is tuned to, for an application of a channel
		self.ait = ait  # the AIT of that service (AitCollection): given to the engine, which starts the autostart application / the one chosen
		self.aitSent = {}  # section number -> the section wpe_fb has been given
		self.pressRed = pressRed  # started by the red button: press it for the user once the application is there
		self.overlay = overlay  # runs on top of the live picture and enigma2's own screen (autostart): not a screen of its own, keys by key set
		self.keyset = 0  # the keys the application has asked for (event "keyset"), see claimedKeys()
		self.redTimer = timer(lambda: self.sendKey("red"))
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

		self.killTimer = timer(self.forceKill)

		# events from wpe_fb (see "decoder handover" there): it writes "acquire" when media needs the decoder and "release" when
		# it is done with it; polled, an enigma2 timer being the simplest way to watch a FIFO
		self.evtFd = None
		self.evtBuf = b""
		self.evtTimer = timer(self.pollEvents)
		self.freeTimer = timer(self.decoderFree)

		# starting takes a few seconds (wpe_fb and its web process, then the page): say so until wpe_fb reports the first page ("loaded"),
		# or after 30 s whatever happens
		self["loading"] = Label(_("Loading HbbTV ..."))
		if overlay:
			self["overlay"] = Pixmap()  # the visible part of the application's frame (showOverlay)
			self.overlaySeq = 0
			self.overlayShown = False  # the widget is on the screen
			self.overlayGeometry = None  # (x, y, w, h) of the picture on the screen; None while the screen is 1x1
			self.overlayFull = False  # the application grew too big for pictures: wpe_fb draws it into the framebuffer now
		self.loadingTimer = timer(self.hideLoading)

		# the streams of the service, for the application: sent to wpe_fb whenever they change (the tracks of a channel change with the
		# programme, and when the application selects one)
		self.components = ""  # what wpe_fb was last told
		self.audioTracks = {}  # audio pid -> enigma2's track number
		self.subtitleTracks = {}  # subtitle pid -> enigma2's subtitle tuple
		self.defaultAudio = None  # the audio pid in use when the application started: where "restore the default" goes back to
		self.componentsTimer = timer(self.pollComponents)
		self.aitTimer = timer(self.pollAit)

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
