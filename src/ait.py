# The AIT of the tuned service: the table that lists the HbbTV applications it signals, read from the demux by the plugin itself.
import fcntl
import os
import queue
import re
import select
import struct
import threading
import time

from enigma import iServiceInformation


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
		threading.Thread.__init__(self, name="HbbTV2-AIT")
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
		self.lock = threading.Lock()  # the prefetch thread (ChannelPrefetch) and the main thread both read the reader's events
		self.parsed = None  # apps(), until a section changes
		self.changes = 0  # how many sections have arrived or changed so far

	def drain(self):
		"""Take over what the reader found since the last call: the new or changed sections."""
		changed = []
		with self.lock:
			while True:
				try:
					event = self.reader.events.get_nowait()
				except queue.Empty:
					return changed
				if event[0] == "section":
					self.pid = event[1]
					self.sections[event[2][6]] = event[2]
					self.parsed = None
					self.changes += 1
					changed.append(event[2])
				else:
					self.failed = True

	def complete(self):
		with self.lock:
			if not self.sections:
				return False
			last = next(iter(self.sections.values()))[7]  # last_section_number
			return all(number in self.sections for number in range(last + 1))

	def apps(self):
		with self.lock:
			if self.parsed is None:
				found = []
				for number in sorted(self.sections):
					try:
						found.extend(parseAit(self.sections[number]))
					except (struct.error, IndexError):  # a section whose lengths run past its end: skipped
						pass
				self.parsed = found
			return list(self.parsed)

	def text(self):
		"""The sections as the lines "<pid> <sid> <hex>" that run_wpe.sh feeds to wpe_fb."""
		return "".join("%d %d %s\n" % (self.pid, self.channel[2], self.sections[n].hex()) for n in sorted(self.sections))

	def stop(self):
		self.reader.stop()
