# -*- coding: utf-8 -*-
# WPE HbbTV test plugin (HbbTV 2.0 experiment)
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
import fcntl
import glob
import os
import queue
import re
import select
import struct
import threading
import time
from sys import maxsize

from enigma import eActionMap, eConsoleAppContainer, ePoint, eSize, eTimer, getDesktop, iPlayableService, iServiceInformation
from Components.ActionMap import ActionMap
from Components.config import ConfigSelection, ConfigSubsection, ConfigText, ConfigYesNo, config, configfile
from Components.Sources.StaticText import StaticText
from Components.Pixmap import Pixmap
from Plugins.Plugin import PluginDescriptor
from Screens.ChoiceBox import ChoiceBox
from Components.Label import Label
from Screens.MessageBox import MessageBox
from Screens.Screen import Screen
from Screens.Setup import Setup

BASE = os.path.dirname(os.path.abspath(__file__))
RUN = f"{BASE}/run_wpe.sh"
PAGES = f"{BASE}/pages"
CTL = "/tmp/wpe_fb.ctl"
EVT = "/tmp/wpe_fb.evt"
LOG = "/tmp/wpe_fb.log"
RED_DELAY = 1500  # ms between "the first page has loaded" and the red key that the red button gives the application
OVERLAY_FILES = "/tmp/wpe_overlay"  # wpe_fb --overlay-png writes /tmp/wpe_overlay_<n>.png (run_wpe.sh)
AIT_FILE = "/tmp/wpe_ait.txt"  # the AIT sections of the service, "<pid> <sid> <hex>" per line, read by run_wpe.sh
CONF = "/etc/enigma2/wpehbbtv.conf"  # KEY=VALUE lines, read by run_wpe.sh (see there for the defaults)
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


# --- the streams of the tuned service, for the application (Broadcast.getComponents and friends) ----------------------------------
# enigma2 knows them (PMT): the video PID and type, the audio tracks with language, PID and format, the subtitle tracks. They go to wpe_fb
# as one line of the "components" control command (see orb_host.h there: V:<pid>:<encoding>:<encrypted>:<aspect>;A:<pid>:<language>:
# <encoding>:<channels>:<active>:<audio description>;S:<pid>:<language>:<encoding>:<active>:<hearing impaired>).

VIDEO_ENCODINGS = {0: "MPEG-2", 1: "H.264", 2: "MPEG-1", 3: "MPEG-4", 4: "VC-1", 5: "VC-1", 6: "H.265"}  # enigma2's sVideoType


def tokenOf(text, default):
	"""text reduced to what may travel in the components line (no ':' or ';', no spaces)."""
	return re.sub(r"[^A-Za-z0-9.+-]", "", text or "") or default


def audioEncoding(description):
	d = (description or "").upper().replace(" ", "")
	if "EAC3" in d or "E-AC3" in d or "DDP" in d:
		return "E-AC3"
	if "AC3" in d:
		return "AC3"
	if "HE-AAC" in d or "AAC+" in d or "HEAAC" in d:
		return "HEAAC"
	if "AAC" in d:
		return "AAC"
	if "DTS" in d:
		return "DTS"
	if "MP3" in d:
		return "MP3"
	if "MPEG" in d:
		return "MPEG1_L2"
	if "PCM" in d:
		return "LPCM"
	return tokenOf(description, "unknown")


def languageOf(code):
	"""ISO 639-2 code of an audio/subtitle track ('deu', 'eng', ... 'und' when enigma2 has none)."""
	m = re.match(r"[A-Za-z]{2,3}", (code or "").strip())
	return m.group(0).lower() if m else "und"


def readComponents(service):
	"""The streams of a running service: (line for the "components" command, {audio pid: track number}, {subtitle pid: enigma2's tuple}, pid of the audio track in use)."""
	info = service.info()
	records = []
	audioTracks = {}
	subtitleTracks = {}
	currentAudio = None
	videoPid = info.getInfo(iServiceInformation.sVideoPID)
	if videoPid is not None and videoPid > 0:
		encoding = VIDEO_ENCODINGS.get(info.getInfo(iServiceInformation.sVideoType), "H.264")
		crypted = 1 if (info.getInfo(iServiceInformation.sIsCrypted) or 0) > 0 else 0
		aspect = info.getInfo(iServiceInformation.sAspect)
		records.append("V:%d:%s:%d:%d" % (videoPid, encoding, crypted, aspect if aspect is not None and 0 <= aspect <= 3 else 1))
	audio = service.audioTracks()
	if audio:
		current = audio.getCurrentTrack()
		for number in range(audio.getNumberOfTracks()):
			track = audio.getTrackInfo(number)
			pid = track.getPID()
			if pid is None or pid <= 0 or pid in audioTracks:
				continue
			audioTracks[pid] = number
			if number == current:
				currentAudio = pid
			channels = track.getChannels() if hasattr(track, "getChannels") else 0
			records.append("A:%d:%s:%s:%d:%d:0" % (pid, languageOf(track.getLanguage()), audioEncoding(track.getDescription()), channels or 0, 1 if number == current else 0))
	subtitle = service.subtitle()
	tracks = subtitle.getSubtitleList() if subtitle else None
	if tracks:
		try:
			from Screens.InfoBar import InfoBar
			selected = InfoBar.instance and InfoBar.instance.selected_subtitle
		except Exception:
			selected = None
		for track in tracks:
			pid = track[1]
			if pid is None or pid <= 0 or pid in subtitleTracks:
				continue
			subtitleTracks[pid] = track
			records.append("S:%d:%s:%s:%d:0" % (pid, languageOf(track[4]), {0: "DVB", 1: "TELETEXT"}.get(track[0], "OTHER"), 1 if selected and selected[1] == pid else 0))
	return ";".join(records), audioTracks, subtitleTracks, currentAudio


PLANE = "/proc/stb/vmpeg/0/dst_%s"
PLANE_NAMES = ("left", "top", "width", "height")
PLANE_BOUNCE = ("00000010", "00000010", "000002b0", "00000220")  # any window that differs from full screen


def readPlane():
	try:
		return [open(PLANE % name).read().strip() for name in PLANE_NAMES]
	except OSError:
		return None


DECODER_DEVICES = ("/dev/dvb/adapter0/video0", "/dev/dvb/adapter0/audio0")


def decoderBusy():
	"""Does any process other than enigma2 have the video or audio decoder open? (enigma2 itself has neither while the service is
	stopped.) The browser's release event comes a moment before its web process has really let go of them, and a service that is
	started meanwhile is ignored by the driver ("VIDEO0: ignored request to start xpt ... its already running") and then killed
	when the last user closes the decoder: the live picture freezes on the last frame of the video."""
	me = str(os.getpid())
	try:
		pids = [name for name in os.listdir("/proc") if name.isdigit() and name != me]
	except OSError:
		return False
	for pid in pids:
		try:
			for fd in os.listdir("/proc/%s/fd" % pid):
				try:
					if os.readlink("/proc/%s/fd/%s" % (pid, fd)) in DECODER_DEVICES:
						return True
				except OSError:
					pass
		except OSError:
			continue
	return False


PLANE_FULL = ("00000000", "00000000", "000002d0", "00000240")  # the whole output: enigma2's own full-screen window


def sanePlane(values):
	"""The video window to go back to. Whatever the window is when the decoder is taken over is not to be trusted: it may be
	that of a mini-TV in an enigma2 menu (the Extensions menu this plugin is started from has one), and restoring that leaves the
	live picture in a corner - for good, since the next session would remember it again. A window that is not (nearly) the
	whole output is replaced by the full-screen one."""
	try:
		if values and int(values[2], 16) >= 0x2b0 and int(values[3], 16) >= 0x220:
			return values
	except ValueError:
		pass
	return list(PLANE_FULL)


def writePlane(values):
	try:
		for name, value in zip(PLANE_NAMES, values):
			open(PLANE % name, "w").write(value + "\n")
		open(PLANE % "apply", "w").write("1\n")
	except OSError:
		pass


class VideoHandover:
	"""Hardware video (dvbvideosink/dvbaudiosink in the browser) needs the video and audio decoder, which enigma2 holds for
	the live service (/dev/dvb/adapter0/video0 and audio0, and the sinks open exactly those): stop the service first, and
	afterwards play it again. The driver of the dm9xx ignores a plain full-screen window while the decoder was not running
	(see eVideoWidget::updatePosition), so after the service is back the window is "bounced": first a different one, then the
	remembered one."""

	def __init__(self, session):
		self.session = session
		self.ref = None
		self.plane = None
		self.active = False
		self.step = 0
		self.waited = 0
		self.timer = eTimer()
		self.timer.callback.append(self.bounce)
		self.playTimer = eTimer()
		self.playTimer.callback.append(self.playWhenFree)

	def acquire(self):
		if self.active:
			return
		if self.playTimer.isActive():
			# the service has not been played again yet (a new video started right away): it is still stopped, keep the
			# reference and the window that were remembered
			self.playTimer.stop()
			self.timer.stop()
			self.active = True
			return
		nav = self.session.nav
		self.ref = nav.getCurrentlyPlayingServiceOrGroup() or nav.getCurrentlyPlayingServiceReference()
		self.plane = sanePlane(readPlane())
		nav.stopService()
		self.active = True

	def release(self):
		if not self.active:
			return
		self.active = False
		self.waited = 0
		self.playTimer.start(100, True)

	def playWhenFree(self):
		if decoderBusy() and self.waited < 6000:
			self.waited += 200
			self.playTimer.start(200, True)
			return
		if self.ref:
			self.session.nav.playService(self.ref)
		self.step = 0
		self.timer.start(4000, True)  # the decoder has to be running again before the driver takes the new window

	def bounce(self):
		if not self.plane:
			return
		if self.step == 0:
			self.step = 1
			writePlane(PLANE_BOUNCE)
			self.timer.start(1000, True)
		else:
			writePlane(self.plane)


handovers = []  # a handover has to outlive its screen: its timer finishes the job after the screen is gone


class WPEScreen(Screen):
	# skin is set in main() from the real desktop size (same pattern as DVDOverlay)
	skin = """<screen name="WPEScreen" position="0,0" size="1920,1080" flags="wfNoBorder" backgroundColor="transparent">
		<widget name="loading" position="640,495" size="640,90" font="Regular;36" halign="center" valign="center" foregroundColor="#ffffff" backgroundColor="#c0101820" zPosition="10" />
	</screen>"""

	def __init__(self, session, page, hwvideo=False, channel=None, ait=None, pressRed=False, overlay=False):
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
		self["actions"] = ActionMap(["WPEHbbTVActions"], actions, -1)

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
		self["loading"] = Label("Loading HbbTV ...")
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
			self.session.open(MessageBox, f"{RUN} not found", MessageBox.TYPE_ERROR)
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
				print("[WPEHbbTV] cannot write %s: %s" % (AIT_FILE, error))
		if self.container.execute(f'{env}{RUN} "{self.page}" > {LOG} 2>&1') != 0:
			self.session.open(MessageBox, "could not start wpe_fb", MessageBox.TYPE_ERROR)
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
			print("[WPEHbbTV] overlay: %s: %s" % (" ".join(words), error))

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
			print("[WPEHbbTV] cannot make the application modal: %s" % error)

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
			print("[WPEHbbTV] reading the components failed: %s" % error)
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
			print("[WPEHbbTV] selecting %s failed: %s" % (" ".join(words), error))
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
		size = getDesktop(0).size()
		WPEScreen.skin = f"""<screen name="WPEScreen" position="0,0" size="{size.width()},{size.height()}" flags="wfNoBorder" backgroundColor="transparent">
			<widget name="loading" position="{(size.width() - 640) // 2},{(size.height() - 90) // 2}" size="640,90" font="Regular;36" halign="center" valign="center" foregroundColor="#ffffff" backgroundColor="#c0101820" zPosition="10" />
		</screen>"""
		# pages that play video on the hardware plane (hw_*.html) and real applications (URLs, which may play video whenever
		# they like) take the decoder from the live service; the other test pages leave the live picture alone
		page = choice[1]
		hwvideo = page.startswith(("http://", "https://", "ait:")) or os.path.basename(page).startswith("hw_")
		session.open(WPEScreen, page, hwvideo, channel, ait, pressRed)


def main(session, **kwargs):
	if not os.path.isdir(PAGES):
		session.open(MessageBox, f"{PAGES} not found", MessageBox.TYPE_ERROR)
		return
	pages = sorted(name for name in os.listdir(PAGES) if name.endswith(".html"))
	if not pages:
		session.open(MessageBox, f"No .html pages in {PAGES}", MessageBox.TYPE_ERROR)
		return
	choices = [(name[:-5], os.path.join(PAGES, name)) for name in pages]
	session.openWithCallback(lambda choice: pageChosen(session, choice), ChoiceBox, title="WPE HbbTV test page  (EXIT closes the page)", list=choices)


def currentServiceAppUrl(session):
	# enigma2 keeps the autostart application URL of the AIT of the current service (see eDVBServicePMTHandler::AITready)
	service = session.nav.getCurrentService()
	info = service and service.info()
	return (info and info.getInfoString(iServiceInformation.sHBBTVUrl)) or ""


def currentChannel(session):
	"""(original network id, transport stream id, service id, name) of the service enigma2 is tuned to, or None."""
	service = session.nav.getCurrentService()
	info = service and service.info()
	if not info:
		return None
	ids = [info.getInfo(what) for what in (iServiceInformation.sONID, iServiceInformation.sTSID, iServiceInformation.sSID)]
	if any(value is None or value < 0 for value in ids):
		return None
	name = re.sub(r"[^\w .+&()-]", "", info.getName() or "").strip()  # goes into a shell command and a line of a FIFO
	return (ids[0], ids[1], ids[2], name or "channel")


# --- the real AIT ---------------------------------------------------------------------------------------------------------------
# A HbbTV channel signals its applications in the AIT (application information table), on a PID that the PMT names with an
# application_signalling_descriptor. enigma2 only keeps the URL of the autostart application; the engine (ORB) wants the whole table - which
# applications there are and with what control code (AUTOSTART, PRESENT, KILL ...) - to start the autostart application, to resolve
# dvb://current.ait/<org>.<app> links and to kill what the channel does not signal any more. So the plugin reads PMT and AIT itself from
# the demux (a section filter of its own next to enigma2's; tools/box/ait_probe.py shows what it finds) and hands the raw sections to wpe_fb.

DMX_SET_FILTER = 0x403C6F2B  # _IOW('o', 43, struct dmx_sct_filter_params): pid, filter[16], mask[16], mode[16], 2 bytes padding, timeout, flags = 60 bytes
DMX_SET_BUFFER_SIZE = 0x6F2D  # _IO('o', 45): enigma2 sets a buffer before every section filter; this driver delivers nothing without
DMX_CHECK_CRC, DMX_IMMEDIATE_START = 1, 4
AIT_CONTROL_CODES = {1: "AUTOSTART", 2: "PRESENT", 3: "DESTROY", 4: "KILL", 5: "PREFETCH", 6: "REMOTE", 7: "UNBOUND"}
AIT_TIMEOUT = 6.0  # seconds to wait for the PMT and the AIT of a service before giving up


def descriptorsOf(data):
	i = 0
	while i + 2 <= len(data):
		yield data[i], data[i + 2:i + 2 + data[i + 1]]
		i += 2 + data[i + 1]


def aitPidOfPmt(section):
	"""The PID of the AIT that a PMT section signals (an elementary stream with an application_signalling_descriptor), or None."""
	i = 12 + (((section[10] & 0x0F) << 8) | section[11])  # after the program info
	end = len(section) - 4  # the CRC
	while i + 5 <= end:
		pid = ((section[i + 1] & 0x1F) << 8) | section[i + 2]
		infoLength = ((section[i + 3] & 0x0F) << 8) | section[i + 4]
		if any(tag == 0x6F for tag, _ in descriptorsOf(section[i + 5:i + 5 + infoLength])):
			return pid
		i += 5 + infoLength
	return None


def parseAit(section):
	"""The applications one AIT section signals: [{"org", "app", "control" (AIT_CONTROL_CODES key), "names": [(language, name)], "http": [url base], "path"}]."""
	apps = []
	i = 10 + (((section[8] & 0x0F) << 8) | section[9])  # after the common descriptors
	end = i + 2 + (((section[i] & 0x0F) << 8) | section[i + 1])
	i += 2
	while i + 9 <= end:
		org, app, control = struct.unpack(">IHB", section[i:i + 7])
		length = ((section[i + 7] & 0x0F) << 8) | section[i + 8]
		info = {"org": org, "app": app, "control": control, "names": [], "http": [], "path": ""}
		for tag, data in descriptorsOf(section[i + 9:i + 9 + length]):
			if tag == 0x01:  # application_name_descriptor
				j = 0
				while j + 4 <= len(data):
					info["names"].append((data[j:j + 3].decode("latin1"), data[j + 4:j + 4 + data[j + 3]].decode("utf-8", "replace")))
					j += 4 + data[j + 3]
			elif tag == 0x02 and len(data) > 3 and (data[0] << 8 | data[1]) == 3:  # transport_protocol_descriptor, protocol 3 = HTTP
				info["http"].append(data[4:4 + data[3]].decode("latin1"))
			elif tag == 0x15:  # simple_application_location_descriptor
				info["path"] = data.decode("latin1")
		apps.append(info)
		i += 9 + length
	return apps


def locatorOf(app):
	"""The part after dvb://current.ait/ that names an application of the AIT: organisation id and application id in HEXADECIMAL."""
	return "%x.%x" % (app["org"], app["app"])


def appName(app):
	names = dict(app["names"])
	return names.get("deu") or names.get("eng") or next(iter(names.values()), "%d.%d" % (app["org"], app["app"]))


class AitReader(threading.Thread):
	"""Reads the PMT of a service and then its AIT from the demux, keeping on reading (a new version of a section is reported again).
	events (queue): ("section", pid, bytes) for each new or changed AIT section, ("none",) when the service signals no AIT, ("error", text)."""

	def __init__(self, demux, sid, pmtPid):
		threading.Thread.__init__(self, name="WPEHbbTV-AIT")
		self.daemon = True
		self.demux, self.sid, self.pmtPid = demux, sid, pmtPid
		self.events = queue.Queue()
		self.stopped = threading.Event()

	def openFilter(self, pid, tableId, extension=None):
		fd = os.open("/dev/dvb/adapter0/demux%d" % self.demux, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
		try:
			fcntl.ioctl(fd, DMX_SET_BUFFER_SIZE, 8192 * 8)
			filt, mask = bytearray(16), bytearray(16)
			filt[0], mask[0] = tableId, 0xFF
			if extension is not None:  # table_id_extension (the program number of a PMT)
				filt[1], filt[2], mask[1], mask[2] = extension >> 8, extension & 0xFF, 0xFF, 0xFF
			fcntl.ioctl(fd, DMX_SET_FILTER, struct.pack("<H16s16s16s2xII", pid, bytes(filt), bytes(mask), bytes(16), 0, DMX_CHECK_CRC | DMX_IMMEDIATE_START))
		except Exception:
			os.close(fd)
			raise
		return fd

	def sections(self, fd, seconds):
		"""Sections arriving on fd for at most `seconds` (None = until stopped)."""
		end = None if seconds is None else time.time() + seconds
		while not self.stopped.is_set() and (end is None or time.time() < end):
			if select.select([fd], [], [], 0.5)[0]:
				try:
					yield os.read(fd, 4096)
				except BlockingIOError:
					pass

	def run(self):
		try:
			aitPid = None
			fd = self.openFilter(self.pmtPid, 0x02, self.sid)
			try:
				for section in self.sections(fd, AIT_TIMEOUT):
					aitPid = aitPidOfPmt(section)
					break
			finally:
				os.close(fd)
			if self.stopped.is_set():
				return
			if aitPid is None:
				self.events.put(("none",))
				return
			latest = {}
			fd = self.openFilter(aitPid, 0x74)
			try:
				for section in self.sections(fd, None):
					if latest.get(section[6]) != section:  # section_number: new, or a new version of it
						latest[section[6]] = section
						self.events.put(("section", aitPid, section))
			finally:
				os.close(fd)
		except Exception as error:
			self.events.put(("error", str(error)))

	def stop(self):
		self.stopped.set()


def startAitReader(session):
	"""(reader, (onid, tsid, sid, name)) for the service enigma2 is playing, or None when it cannot be read (no service, no PMT PID)."""
	channel = currentChannel(session)
	service = session.nav.getCurrentService()
	info = service and service.info()
	if not channel or not info:
		return None
	pmtPid = info.getInfo(iServiceInformation.sPMTPID)
	if pmtPid is None or pmtPid <= 0:
		return None
	try:
		demux = int(info.getInfoString(iServiceInformation.sLiveStreamDemuxId))
	except (AttributeError, ValueError, TypeError):
		demux = 0
	reader = AitReader(max(demux, 0), channel[2], pmtPid)
	reader.start()
	return reader, channel


class AitCollection:
	"""The sections of the AIT of a service as they arrive (drain() reads the reader's events)."""

	def __init__(self, reader, channel):
		self.reader, self.channel = reader, channel
		self.pid = None
		self.sections = {}  # section_number -> bytes
		self.failed = False  # no AIT signalled, or it could not be read

	def drain(self):
		"""Take over what the reader found since the last call: the new or changed sections."""
		changed = []
		while True:
			try:
				event = self.reader.events.get_nowait()
			except queue.Empty:
				return changed
			if event[0] == "section":
				self.pid = event[1]
				self.sections[event[2][6]] = event[2]
				changed.append(event[2])
			else:
				self.failed = True

	def complete(self):
		if not self.sections:
			return False
		last = next(iter(self.sections.values()))[7]  # last_section_number
		return all(number in self.sections for number in range(last + 1))

	def apps(self):
		found = []
		for number in sorted(self.sections):
			found.extend(parseAit(self.sections[number]))
		return found

	def text(self):
		"""The sections as the lines "<pid> <sid> <hex>" that run_wpe.sh feeds to wpe_fb."""
		return "".join("%d %d %s\n" % (self.pid, self.channel[2], self.sections[n].hex()) for n in sorted(self.sections))

	def stop(self):
		self.reader.stop()


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
		try:
			started = startAitReader(self.session)
		except Exception as error:
			print("[WPEHbbTV] cannot read the AIT: %s" % error)
			started = None
		if not started:
			self.fallback()
			return
		self.ait = AitCollection(*started)
		self.box = None  # no "Reading the HbbTV application information" message box: the AIT is read silently
		self.timer.start(200, False)

	def closeBox(self):
		if self.box:
			try:
				self.box.close()
			except Exception:
				pass
			self.box = None

	def poll(self):
		self.ait.drain()
		self.waited += 0.2
		if self.ait.complete() or self.ait.failed or self.waited >= AIT_TIMEOUT + 1:
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
		if len(apps) == 1 or (autostart and len(apps) == len(autostart)) or (self.redButton and autostart):
			self.launch(autostart[0] if autostart else apps[0])  # only one to choose (the red button always takes the autostart application)
			return
		entries = [("%s%s" % (appName(app), "  (autostart)" if app["control"] == 1 else ""), app) for app in sorted(apps, key=lambda a: a["control"] != 1)]
		self.session.openWithCallback(self.picked, ChoiceBox, title="HbbTV applications of %s  (EXIT closes)" % self.ait.channel[3], list=entries)

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
			self.session.open(MessageBox, "The current channel does not signal an HbbTV application (no AIT).", MessageBox.TYPE_INFO, timeout=5)
			return
		pageChosen(self.session, (url, url), currentChannel(self.session), pressRed=self.redButton)


launchers = []  # a launcher has to stay alive while it waits for the AIT
screens = []  # the application screens that are open (WPEScreen adds itself, cleanup() removes it)


def channelApp(session, redButton=False, **kwargs):
	if screens or any(controller.overlay for controller in autostart):  # an application is on the screen already
		return
	launcher = ChannelAppLauncher(session, redButton)
	launchers[:] = [launcher]
	launcher.start()


def prepareSkin():
	"""The skin of the automatic banner: a transparent screen with one Pixmap widget, which shows the visible part of the application's frame (a PNG
	from wpe_fb, see WPEScreen.showOverlay) on top of everything enigma2 has on the screen. The screen is 1x1 until there is something to show, and
	then only as big as that (moved and resized in showOverlay): a window over the whole desktop makes enigma2 redraw everything below it, and the
	infobar flickered when the banner started."""
	WPEScreen.skin = """<screen name="WPEScreen" position="0,0" size="1,1" flags="wfNoBorder" backgroundColor="transparent">
		<widget name="overlay" position="0,0" size="1,1" zPosition="5" alphatest="blend" />
	</screen>"""


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
		try:
			started = startAitReader(self.session)
		except Exception as error:
			print("[WPEHbbTV] autostart: cannot read the AIT: %s" % error)
			return
		if started:
			self.collection = AitCollection(*started)
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
			prepareSkin()
			overlay = self.session.instantiateDialog(WPEScreen, "ait:", True, collection.channel, collection, False, True)
		except Exception as error:
			print("[WPEHbbTV] autostart: cannot create the overlay: %s" % error)
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
	HbbTV plugins used). The InfoBar does not exist yet when the session starts: register as soon as it does."""

	def __init__(self, session):
		self.session = session
		self.tries = 0
		self.timer = eTimer()
		self.timer.callback.append(self.register)
		self.timer.start(2000, False)

	def register(self):
		self.tries += 1
		try:
			from Screens.InfoBar import InfoBar
			if InfoBar.instance is not None:
				InfoBar.instance.onHBBTVActivation.append(self.activated)
				self.timer.stop()
		except Exception as error:
			print("[WPEHbbTV] red button hook: %s" % error)
		if self.tries > 90:
			self.timer.stop()

	def activated(self):
		if readSettings()["WPE_REDBUTTON"] == "1":  # not reached while a running automatic application has taken the red key
			channelApp(self.session, redButton=True)


hooks = []  # what the session start set up, kept alive


autostart = []  # the controller of the automatic banner (one)


def sessionStart(reason, session=None, **kwargs):
	if reason == 0 and session is not None and not hooks:
		hooks.append(RedButtonHook(session))
		autostart.append(AutoStart(session))



# --- settings ------------------------------------------------------------------------------------------------
# One row per flag of /etc/enigma2/wpehbbtv.conf that run_wpe.sh reads:
#   (key, title, [(value, text), ...] or None for a text field, default, help)
# "default" is what run_wpe.sh / wpe_fb use when the key is absent from the conf: a value equal to it is not written, so the conf only holds what
# was changed (and "Reset to defaults" empties it). An option with the value "" stands for "not set: leave it to WebKit / the driver".
# Settings that are not in a group below are kept in the conf untouched.
ON_OFF = [("0", "off"), ("1", "on")]


def msOptions(*values):
	return [(str(value), "%d ms" % value if value else "off") for value in values]


SETTING_GROUPS = [
	("Application", [
		("WPE_REDBUTTON", "Red button starts the application", [("1", "on"), ("0", "off")], "1",
			"The red button on a channel with an HbbTV application starts it (and gives it the red key)."),
		("WPE_AUTOSTART", "Automatic banner when zapping", ON_OFF, "0",
			"Start the AUTOSTART application of a channel by itself, on top of the live picture, a few seconds after zapping."),
		("WPE_PERSIST", "Keep cache, cookies and storage", [("1", "yes"), ("0", "no")], "1",
			"The HTTP cache (up to 75 MB), cookies and local storage of the applications survive between runs. Needs /media/hdd."),
		("WPE_PERSIST_DIR", "Storage directory", None, "",
			"Where they live. Empty = /media/hdd/.wpe-data. An absolute path of letters, digits and _ . / -"),
		("WPE_TOLERANT", "Tolerant mode", ON_OFF, "0",
			"Ignore TLS certificate errors, show no WebKit error page for a failed load, and reload a crashed page instead of quitting."),
		("WPE_DEBUG", "Debug log (slower)", ON_OFF, "0",
			"The page's console, media events, every network request and GStreamer output go to /tmp/wpe_fb.log."),
	]),
	("Rendering", [
		("WPE_HIDPI", "Render quality", [("1.5", "sharp (1.5x)"), ("1.25", "balanced (1.25x)"), ("1.0", "fast (1.0x)")], "1.0",
			"The read-back of a frame costs time in proportion to its pixels: 1.0 reacts about three times quicker than 1.5."),
		("WPE_FAST_ANIM", "Shorter animations", [("1", "on"), ("0", "off")], "1",
			"CSS transitions and animations last 1 ms and there is no smooth scrolling: fewer frames per key press."),
		("WPE_SKIA_CPU", "Page painting", [("1", "CPU"), ("0", "GPU")], "1",
			"CPU painting uses little GPU memory (no black tiles) and is only slightly slower. GPU painting keeps tiles as GPU textures."),
		("WPE_HW_ACCEL", "Software compositing", [("", "off"), ("never", "on")], "",
			"Composite the page in software: no GPU memory is used for page layers."),
	]),
	("GPU memory", [
		("WPE_COVER_AREA", "Tile cover area", [("", "default (2)"), ("1", "1 (visible area only)"), ("1.5", "1.5"), ("2", "2"), ("3", "3"), ("4", "4")], "",
			"Area kept in tiles as a multiple of the visible area, squared (the default 2 keeps four times the visible area). Lower = less GPU memory."),
		("WPE_SKIA_CACHE_MB", "Skia GPU cache", [("", "default")] + [(str(v), "%d MB" % v) for v in (8, 16, 32, 64, 128, 256)], "",
			"Limit of the Skia GPU resource cache."),
		("WPE_TEXTURE_POOL_MB", "Unused tile texture pool", [("", "default")] + [(str(v), "%d MB" % v) for v in (16, 32, 64, 128, 256)], "",
			"Above this size unused tile textures are released after 50 ms instead of 3 s."),
		("WPE_TILE_SIZE", "Tile size (GPU painting)", [("", "default"), ("256x256", "256 x 256"), ("512x256", "512 x 256"), ("512x512", "512 x 512"), ("1024x256", "1024 x 256")], "",
			"The GPU default is as wide as the viewport; smaller tiles allocate less memory per update."),
		("WPE_SKIA_DDL", "Deferred display lists (GPU painting)", [("1", "on"), ("0", "off")], "1",
			"Off: paint with Skia GPU worker threads into pooled textures instead (a different memory use)."),
		("WPE_SKIA_GPU_THREADS", "GPU painting threads", [("", "default")] + [(str(v), str(v)) for v in (1, 2, 3, 4)], "",
			"Painting threads when deferred display lists are off."),
		("WEBKIT_DISABLE_GL_FENCE", "GL fence sync", [("", "automatic"), ("0", "use fences"), ("1", "disable fences")], "",
			"Automatic: fences on Nexus boxes, off on others. Disable them when the driver crashes in glFlush() or has no fence sync."),
	]),
	("Output and keys", [
		("WPE_LIMIT_REPEAT", "Limit key repeat", [("1", "on"), ("0", "off")], "1",
			"Pass the remote's auto-repeat on at most once per delivered frame: a held key moves fewer steps (a matter of feel, not speed)."),
		("WPE_NICE", "Process priority (nice)", [(str(v), str(v)) for v in (-20, -10, -5, 0, 5, 10)], "-20",
			"Scheduling priority of wpe_fb and its web processes: -20 highest, 19 lowest."),
		("WPE_EGL", "Output path", [("", "automatic"), ("1", "enigma2 EGL renderer"), ("0", "framebuffer")], "",
			"Automatic: enigma2's EGL renderer when it listens for the page (Nexus boxes), else the framebuffer."),
	]),
	("Frame timing (EGL output)", [
		("WPE_EGL_SETTLE_MS", "Settle time", msOptions(0, 30, 60, 100, 150, 250), "0",
			"After a change, wait this long for the page to stop changing before showing the frame."),
		("WPE_EGL_MAX_DELAY_MS", "Longest delay", msOptions(0, 100, 250, 500, 1000), "0",
			"A frame is never held back longer than this by the settle time."),
		("WPE_EGL_KEY_MS", "Key window", msOptions(0, 200, 400, 800), "400",
			"Frames this soon after a key press are the page's answer to it and are shown at once."),
		("WPE_EGL_HOLD_MS", "Hold solid areas", msOptions(0, 50, 100, 200, 400), "0",
			"Delay solid areas of a frame to hide a flash (can show as a late flash after an animation)."),
		("WPE_EGL_HOLD_CLEAR", "Hold cleared areas", ON_OFF, "0", "Apply the hold to cleared (transparent) areas too."),
		("WPE_EGL_DIP_MS", "Dip time", msOptions(0, 150, 300, 600), "300", "Duration of the picture dip copy (see wpe_fb dip_copy)."),
		("WPE_EGL_RESYNC_MS", "Resync time", msOptions(0, 100, 250, 500), "0",
			"Re-read released buffers after this time to repair rows. Off by default: it can bring back older pixels."),
		("WPE_EGL_PIG_FIRST_MS", "Picture-in-graphics: first", msOptions(1000, 2000, 3000, 5000), "3000",
			"Wait before the first video rectangle appears in graphics (a loading animation must not flash the picture)."),
		("WPE_EGL_PIG_SETTLE_MS", "Picture-in-graphics: change", msOptions(100, 300, 500, 1000), "300",
			"Wait before a showing video rectangle moves or goes away."),
		("WPE_EGL_NO_HOLE", "No transparent hole", ON_OFF, "0", "Do not cut a transparent hole for the video in the frame."),
		("WPE_EGL_NO_TRIM", "No row trimming", ON_OFF, "0", "Copy whole damage rectangles instead of trimming unchanged rows."),
		("WPE_EGL_DEBUG_RECTS", "Show damage rectangles", ON_OFF, "0", "Draw the copied rectangles into the frame (debugging)."),
		("WPE_NO_NEXUS_JOIN", "Do not join Nexus", ON_OFF, "0", "Skip the NxClient join (debugging; the page cannot get an EGL display on Nexus boxes without it)."),
	]),
]

SETTINGS = [row for _, rows in SETTING_GROUPS for row in rows]


def settingName(key):
	return key.lower()


def switchValues(options):
	"""(value for off, value for on) of an on/off (yes/no) setting, shown as enigma2's graphical switch (ConfigYesNo); None for any other setting."""
	if options is None or len(options) != 2:
		return None
	texts = dict((text, value) for value, text in options)
	for off, on in (("off", "on"), ("no", "yes")):
		if set(texts) == {off, on}:
			return texts[off], texts[on]
	return None


def isSwitch(options):
	return switchValues(options) is not None


def isOptional(options):
	"""A setting with a choice "off" (value 0) among others (a time that can be switched off): a switch, and the time when it is on."""
	return options is not None and not isSwitch(options) and ("0", "off") in options


def timeOptions(options):
	return [option for option in options if option != ("0", "off")]


def configOf(key, options, default):
	"""The config element of a setting: config.plugins.wpehbbtv.<key>, stored in enigma2's settings like any plugin's."""
	if options is None:
		return ConfigText(default=default, fixed_size=False, visible_width=40)
	if isSwitch(options):
		return ConfigYesNo(default=default == switchValues(options)[1])
	if isOptional(options):
		choices = timeOptions(options)
		return ConfigSelection(choices=choices, default=default if default != "0" else choices[0][0])
	return ConfigSelection(choices=[(option or "-", text) for option, text in options], default=default or "-")  # (the empty option as "-": a key of "" is not safe in every enigma2)


config.plugins.wpehbbtv = ConfigSubsection()
for _key, _title, _options, _default, _help in SETTINGS:
	setattr(config.plugins.wpehbbtv, settingName(_key), configOf(_key, _options, _default))
	if isOptional(_options):  # the switch in front of the time
		setattr(config.plugins.wpehbbtv, settingName(_key) + "_on", ConfigYesNo(default=_default != "0"))


def settingElement(key):
	return getattr(config.plugins.wpehbbtv, settingName(key))


def onElement(key):
	return getattr(config.plugins.wpehbbtv, settingName(key) + "_on")


def setSetting(key, options, value):
	"""Put a conf value into the config element(s) of a setting."""
	try:
		element = settingElement(key)
		if isSwitch(options):
			element.value = value == switchValues(options)[1]
		elif isOptional(options):
			onElement(key).value = value != "0"
			if value != "0":
				element.value = value
		else:
			element.value = value or "-" if options is not None else value
	except Exception:
		pass  # a value this setting does not offer: left as it was


def loadConf():
	"""Show the values of the conf file in the config (a key it does not have is at its default), as the saved state: nothing is "changed" until the user
	changes it. Without a conf file the saved config stays."""
	try:
		with open(CONF) as f:
			conf = dict(line.strip().partition("=")[::2] for line in f if "=" in line)
	except OSError:
		return
	for key, title, options, default, help in SETTINGS:
		setSetting(key, options, conf.get(key, default))
		settingElement(key).save()
		if isOptional(options):
			onElement(key).save()


loadConf()


def valueOf(key, options):
	value = settingElement(key).value
	if isSwitch(options):
		return switchValues(options)[1 if value else 0]
	if isOptional(options):
		return value if onElement(key).value else "0"
	return "" if value == "-" else value.strip()


def readSettings():
	"""{key: value} of every setting, as run_wpe.sh gets it ("" = not set)."""
	return {row[0]: valueOf(row[0], row[2]) for row in SETTINGS}


def persistDir():
	value = readSettings()["WPE_PERSIST_DIR"]
	return value if value.startswith("/") and len(value) > 1 else PERSIST_DIR


def writeSettings():
	"""Write /etc/enigma2/wpehbbtv.conf, which run_wpe.sh reads, from the config. Only what differs from the default is written. Keys this plugin does
	not know (e.g. WEBKIT_* set by hand) are kept as they are."""
	values = readSettings()
	known = {row[0] for row in SETTINGS}
	defaults = {row[0]: row[3] for row in SETTINGS}
	extra = []
	try:
		with open(CONF) as f:
			extra = [line.rstrip("\r\n") for line in f if line.partition("=")[0] not in known and line.strip()]
	except OSError:
		pass
	try:
		with open(CONF, "w") as f:
			f.write("".join(f"{key}={value}\n" for key, value in values.items() if value != defaults[key] and value != ""))
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
		self.title = "WPE HbbTV settings"
		self["key_yellow"] = StaticText("Defaults")
		self["key_blue"] = StaticText("Delete stored data")
		self["wpeActions"] = ActionMap(["ColorActions"], {"yellow": self.defaults, "blue": self.clear}, -1)
		self.addSaveNotifier(self.saved)

	def createSetup(self):
		configlist = []
		for number, (group, rows) in enumerate(SETTING_GROUPS):
			if number:
				configlist.append(("---",))
			for key, title, options, default, help in rows:
				if isOptional(options):
					configlist.append((title, onElement(key), help))
					if onElement(key).value:
						configlist.append(("  Time", settingElement(key), help))
				else:
					configlist.append((title, settingElement(key), help))
		self["config"].list = configlist

	def saved(self):
		if not writeSettings():
			self.session.open(MessageBox, f"Could not write {CONF}", MessageBox.TYPE_ERROR)

	def defaults(self):
		self.session.openWithCallback(self.defaultsConfirmed, MessageBox, "Set every setting to its default?", MessageBox.TYPE_YESNO, default=False)

	def defaultsConfirmed(self, yes):
		if yes:
			for key, title, options, default, help in SETTINGS:
				setSetting(key, options, default)
			self.createSetup()

	def clear(self):
		self.session.openWithCallback(lambda yes: clearStoredData(self.session) if yes else None, MessageBox,
			f"Delete the stored cache, cookies and local storage of the HbbTV applications ({persistDir()})?", MessageBox.TYPE_YESNO, default=False)


def settingsMenu(session, **kwargs):
	session.open(WPESettings)


def clearStoredData(session):
	import shutil
	directory = persistDir()
	try:
		shutil.rmtree(directory)
		session.open(MessageBox, "Stored data deleted.", MessageBox.TYPE_INFO, timeout=4)
	except FileNotFoundError:
		session.open(MessageBox, "There is nothing stored.", MessageBox.TYPE_INFO, timeout=4)
	except OSError as e:
		session.open(MessageBox, f"Could not delete {directory}: {e}", MessageBox.TYPE_ERROR)


def Plugins(**kwargs):
	return [PluginDescriptor(name="WPE HbbTV Test", description="WPE WebKit HbbTV 2.0 experiment: test pages on top of enigma2",
		where=PluginDescriptor.WHERE_EXTENSIONSMENU, fnc=main),
		PluginDescriptor(name="WPE HbbTV: channel app", description="Run the HbbTV application of the current channel (ORB engine, mock platform)",
		where=PluginDescriptor.WHERE_EXTENSIONSMENU, fnc=channelApp),
		PluginDescriptor(name="WPE HbbTV: settings", description="Red button, autostart, rendering, GPU memory, timing and stored data",
		where=PluginDescriptor.WHERE_EXTENSIONSMENU, fnc=settingsMenu),
		PluginDescriptor(where=PluginDescriptor.WHERE_SESSIONSTART, fnc=sessionStart)]
