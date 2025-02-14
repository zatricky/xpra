#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# This file is part of Xpra.
# Copyright (C) 2023 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import re
from enum import IntEnum

from xpra.exit_codes import ExitCode
from xpra.util import typedict
from xpra.dbus.common import loop_init, init_session_bus
from xpra.dbus.helper import dbus_to_native
from xpra.codecs.gstreamer.capture import Capture
from xpra.server.shadow.root_window_model import RootWindowModel
from xpra.server.shadow.gtk_shadow_server_base import GTKShadowServerBase
from xpra.log import Logger

log = Logger("shadow")

BASE_REQUEST_PATH = "/org/freedesktop/portal/desktop/request"
PORTAL_REQUEST = "org.freedesktop.portal.Request"
PORTAL_DESKTOP_INTERFACE = "org.freedesktop.portal.Desktop"
PORTAL_DESKTOP_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"

loop_init()
bus = init_session_bus()


class AvailableSourceTypes(IntEnum):
    MONITOR = 1
    WINDOW = 2
    VIRTUAL = 4


class ScreenCast(GTKShadowServerBase):
    def __init__(self, multi_window=True):
        GTKShadowServerBase.__init__(self, multi_window=multi_window)
        self.session_type : str = "pipewire shadow"
        self.capture : Capture = None
        self.request_counter : int = 0
        self.session_counter : int = 0
        self.session_handler = 0
        self.portal_interface = None
        self.dbus_sender_name : str = re.sub(r'\.', r'_', bus.get_unique_name()[1:])
        self.portal_interface = bus.get_object(PORTAL_DESKTOP_INTERFACE, PORTAL_DESKTOP_PATH)
        log(f"setup_capture() self.portal_interface={self.portal_interface}")

    #def init(self, opts):
    #    GTKShadowServerBase.init(self, opts)

    def notify_new_user(self, ss):
        log("notify_new_user() start capture")
        super().notify_new_user(ss)
        if not self._window_to_id:
            self.dbus_request_screenscast()

    def last_client_exited(self):
        super().last_client_exited()
        c = self.capture
        if c:
            self.capture = None
            c.stop()


    def makeRootWindowModels(self):
        log("makeRootWindowModels()")
        return []

    def makeDynamicWindowModels(self):
        log("makeDynamicWindowModels()")
        return []

    def set_keymap(self, server_source, force=False):
        log.info("keymap support not implemented in pipewire screencast shadow server")

    def setup_capture(self):
        pass

    def cleanup(self):
        GTKShadowServerBase.cleanup(self)
        self.portal_interface = None

    def start_refresh(self, wid):
        self.start_capture()

    def start_capture(self):
        pass

    def screen_cast_call(self, method, callback, *args, options={}):
        #generate a new token and path:
        self.request_counter += 1
        request_token = f"u{self.request_counter}"
        request_path = f"{BASE_REQUEST_PATH}/{self.dbus_sender_name}/{request_token}"
        log(f"adding dbus signal receiver {callback}")
        bus.add_signal_receiver(callback,
                                'Response',
                                PORTAL_REQUEST,
                                PORTAL_DESKTOP_INTERFACE,
                                request_path)
        options["handle_token"] = request_token
        log(f"calling {method} with args={args}, options={options}")
        method(*(args + (options, )), dbus_interface=SCREENCAST_IFACE)


    def dbus_request_screenscast(self):
        self.session_counter += 1
        options = {
            "session_handle_token"  : f"u{self.session_counter}",
            }
        self.screen_cast_call(
            self.portal_interface.CreateSession,
            self.on_create_session_response,
            options=options,
            )

    def on_create_session_response(self, response, results):
        r = int(response)
        res = typedict(dbus_to_native(results))
        if r:
            log.error("on_create_session_response", (response, results))
            log.error(f"Error {r} creating the session")
            self.quit(ExitCode.UNSUPPORTED)
            return
        self.session_handle = res.strget("session_handle")
        log("on_create_session_response%s session_handle=%s", (r, res), self.session_handle)
        if not self.session_handle:
            log.error("Error: missing session handle creating the session")
            self.quit(ExitCode.UNSUPPORTED)
            return
        from dbus.types import UInt32
        options = {
            "multiple"  : self.multi_window,
            "types"     : UInt32(AvailableSourceTypes.WINDOW | AvailableSourceTypes.MONITOR),
            }
        log(f"on_create_session_response calling SelectSources with options={options}")
        self.screen_cast_call(
            self.portal_interface.SelectSources,
            self.on_select_sources_response,
            self.session_handle,
            options=options)

    def on_select_sources_response(self, response, results):
        r = int(response)
        res = typedict(dbus_to_native(results))
        if r:
            log("on_select_sources_response%s", (response, results))
            log.error(f"Error {r} selecting sources")
            self.quit(ExitCode.UNSUPPORTED)
            return
        log(f"on_select_sources_response sources selected, results={res}")
        self.screen_cast_call(
            self.portal_interface.Start,
            self.on_start_response,
            self.session_handle,
            "")

    def on_start_response(self, response, results):
        r = int(response)
        res = typedict(dbus_to_native(results))
        if r:
            log.error("on_start_response%s", (response, results))
            log.error(f"Error {r} starting the screen capture")
            self.quit(ExitCode.UNSUPPORTED)
            return
        streams = res.tupleget("streams")
        if not streams:
            log.error("Error: failed to start capture:")
            log.error(" missing streams")
            self.quit(ExitCode.UNSUPPORTED)
            return
        log(f"on_start_response starting pipewire capture for {streams}")
        for node_id, props in streams:
            #start_thread(self.start_pipewire_capture,
            #             f"start-pipewire-capture-{node_id}",
            #             daemon=True,
            #             args = (node_id, typedict(props)))
            self.start_pipewire_capture(node_id, typedict(props))

    def start_pipewire_capture(self, node_id, props):
        log(f"start_pipewire_capture({node_id}, {props})")
        from dbus.types import Dictionary
        empty_dict = Dictionary(signature="sv")
        fd_object = self.portal_interface.OpenPipeWireRemote(self.session_handle,
                                                             empty_dict,
                                                             dbus_interface=SCREENCAST_IFACE)
        fd = fd_object.take()
        #from gi.repository import Gst
        #pipeline = Gst.parse_launch('pipewiresrc fd=%d path=%u ! videoconvert ! xvimagesink'%(fd, node_id))
        #pipeline.set_state(Gst.State.PLAYING)
        #def on_gst_message(*args):
        #    log.info(f"on_gst_message{args}")
        #pipeline.get_bus().connect('message', on_gst_message)
        #return
        x, y = props.inttupleget("position", (0, 0))
        w, h = props.inttupleget("size", (0, 0))
        el = f"pipewiresrc fd={fd} path={node_id}"
        self.capture = Capture(el, pixel_format="BGRX", width=w, height=h)
        self.capture.connect("state-changed", self.capture_state_changed)
        self.capture.connect("error", self.capture_error)
        self.capture.connect("new-image", self.capture_new_image)
        self.capture.start()
        source_type = props.intget("source_type")
        title = f"{AvailableSourceTypes(source_type)} {node_id}"
        geometry = (x, y, w, h)
        model = RootWindowModel(self.root, self.capture, title, geometry)
        #must be called from the main thread:
        log(f"new model: {model}")
        self.idle_add(self._add_new_window, model)

    def capture_new_image(self, capture, frame):
        log(f"capture_new_image({capture}, {frame})")

    def capture_error(self, *args):
        log.warn(f"capture_error{args}")
        self.quit(ExitCode.INTERNAL_ERROR)

    def capture_state_changed(self, *args):
        log.info(f"capture_state_changed{args}")

    def stop_capture(self):
        c = self.capture
        if c:
            self.capture = None
            c.clean()


    def _move_pointer(self, device_id, wid, pos, props=None):
        #x, y = pos
        pass

    def do_process_button_action(self, *args):
        pass
