# -*- coding: utf-8 -*-
# HbbTV 2.0 plugin: HbbTV 2.0 test pages and applications on top of enigma2
#
# Shows a WPE WebKit page on top of enigma2 (see screen.py) and offers it: the test pages, the channel's application,
# the automatic start and the red button.
#
# Files it expects next to this plugin (installed with it):
#   run_wpe.sh   starts wpe_fb with the right environment
#   pages/*.html the pages offered in the chooser
import os

from Plugins.Plugin import PluginDescriptor
from Screens.ChoiceBox import ChoiceBox
from Screens.MessageBox import MessageBox

from . import _
from .channel import AutoStart, ChannelPrefetch, RedButtonHook, autostart, channelApp, prefetchers
from .screen import PAGES, pageChosen
from .settings import settingsMenu


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


hooks = []  # what the session start set up, kept alive


def sessionStart(reason, session=None, **kwargs):
	if reason == 0 and session is not None and not hooks:
		hooks.append(RedButtonHook(session))
		autostart.append(AutoStart(session))
		prefetchers.append(ChannelPrefetch(session))


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
