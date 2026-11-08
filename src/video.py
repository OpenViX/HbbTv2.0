# The video side of the plugin: the streams of the tuned service (for the application), and the handover of the video and audio decoder between enigma2
# and the browser.
import os
import re

from enigma import eTimer, iServiceInformation


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
