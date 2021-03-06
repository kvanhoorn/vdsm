#
# Copyright 2009-2017 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

from __future__ import absolute_import

import inspect
import logging
import threading

import pyudev

from collections import namedtuple

from vdsm.storage import devicemapper

MultipathEvent = namedtuple("MultipathEvent",
                            "type, mpath_uuid, path, valid_paths, dm_seqnum")

MPATH_REMOVED = "removed"
PATH_FAILED = "failed"
PATH_REINSTATED = "reinstated"


def create_observer(monitor, callback, name):
    """
    This method is needed in order to support different versions of pyudev.
    The 'callback' parameter has been introduced in 0.16.
    TODO: Remove when using pyudev >= 0.16 on all platforms.
    """
    argspec = inspect.getargspec(pyudev.MonitorObserver.__init__)
    if "callback" in argspec.args:
        # pylint: disable=no-value-for-parameter
        return pyudev.MonitorObserver(monitor,
                                      callback=callback,
                                      name=name)
    else:
        def event_handler(action, device):
            callback(device)
        return pyudev.MonitorObserver(monitor, event_handler, name=name)


class MultipathMonitor(object):
    """
    Interface for multipath monitors.
    """

    def start(self):
        """
        Called when starting multipath listener, after udev events were
        registered with the kernel, but the listener is not reading the events
        yet.

        When all registered monitors were started, the listener starts to
        receive udev events, and the montior handle() method may be called.

        Should be implemented by a monitor if it needs to check the current
        system state before receiving events.
        """

    def handle(self, event):
        """
        Must be implemented by objects registered with MultipathListener.

        Arguments:
            A MultipathEvent namedtuple.
        """
        raise NotImplementedError

    def stop(self):
        """
        Called when the listener was stopped.

        May be implemented by a montior if it wants to do some cleanup when
        stopping monitoring.
        """


class MultipathListener(object):
    log = logging.getLogger("storage.udev")

    def __init__(self):
        self._lock = threading.Lock()
        self._monitors = set()
        self._observer = None

    def start(self):
        """
        Start listening for events and start registered monitors.

        Registered monitors are started after registring events with the
        kernel, but before strarting the observer thread.

        Listening is done in a new observer thread. Event received the observer
        thread are forwarded to registerd monitors in the observer thread.

        Raise:
            Exception if a registered monitor failed to start.
        """
        self.log.info("Starting multipath event listener")
        with self._lock:
            if self._observer is not None:
                raise AssertionError("Listener already started")

            # The monitor is created here so that when the observer is stopped,
            # it will remove the last reference to the monitor,
            # closing the udev connection.
            context = pyudev.Context()
            monitor = pyudev.Monitor.from_netlink(context)
            monitor.filter_by("block", device_type="disk")
            self._observer = create_observer(monitor,
                                             self._callback,
                                             name="mpathlistener")

            # NOTE: order is important!

            # Start the udev monitor, registreing events with the kernel, but
            # do not start the observer yet.
            monitor.start()

            # Start the registered monitors. At this point the monitors can
            # check the initial state of the system, without lossing events.
            self._start_monitors(self._monitors)

            # Once all the monitors started, we can start receiving events from
            # the kernel.
            self._observer.start()

    def stop(self):
        """
        Stop listening for events and stop registerd monitors.
        """
        self.log.info("Stopping multipath event listener")
        with self._lock:
            if self._observer is None:
                return
            self._observer.stop()
            self._observer = None
            self._stop_monitors(self._monitors)

    def register(self, monitor):
        """
        Register a monitor with the listener. The monitor.handle() method will
        be invoked with a MultipathEvent instance when receiving an event from
        udev.

        The monitor.handle() method must never block, blocking will delay
        receiving multipath events for the entire system.  If the monitor need
        to block, it should add the events to a queue and do the blocking
        operation in another thread.

        The caller is responsible to unregister the monitor when it is not
        needed.

        If the listener was arlready started, the monitor will be started at
        this point. The monitor must be able to handle events while it is
        starting.

        Arguments:
            monitor: An object implementing the MultipathMonitor interface.

        Raises:
            Exception if the listener has already started, and the monitor
                failed to start.
        """
        self.log.info("Registering multipath event monitor %s", monitor)

        # NOTE: order is important!

        # First register the monitor, so it will not loose any event while we
        # start it.
        with self._lock:
            if monitor in self._monitors:
                raise AssertionError("Monitor %s already registered" % monitor)

            self._monitors.add(monitor)

            # Not started yet, we are done.
            if self._observer is None:
                return

        # Now start the monitor for checking the initial system state. The
        # monitor must handle events while it is starting.
        self.log.debug("Starting monitor %s", monitor)
        try:
            monitor.start()
        except:
            with self._lock:
                self._monitors.remove(monitor)
            raise

    def unregister(self, monitor):
        self.log.info("Unregistering multipath event monitor %s", monitor)
        with self._lock:
            if monitor not in self._monitors:
                raise AssertionError("Monitor %s not registered" % monitor)
            self._monitors.remove(monitor)
            self._stop_monitors([monitor])

    def _callback(self, device):
        self.log.debug("Received udev event (action=%s, device=%s)",
                       device["ACTION"], device)
        try:
            event = self._detect_event(device)
        except Exception as e:
            self.log.exception("Error detecting udev event: %s", e)
            return

        if event:
            self.log.debug("Forwarding %s", event)
            self._forward_event(event)

    def _detect_event(self, device):
        mpath_uuid = device.get("DM_UUID", "")
        if not mpath_uuid.startswith("mpath-"):
            return None
        mpath_uuid = mpath_uuid[6:]

        if device["ACTION"] == "change":
            dm_action = device.get("DM_ACTION")
            if dm_action == "PATH_FAILED":
                event_type = PATH_FAILED
            elif dm_action == "PATH_REINSTATED":
                event_type = PATH_REINSTATED
            else:
                self.log.debug("Unsupported DM_ACTION %r", dm_action)
                return
            valid_paths = int(device.get("DM_NR_VALID_PATHS"))
            dm_seqnum = int(device.get("DM_SEQNUM"))
            path = devicemapper.device_name(device.get("DM_PATH"))
        elif device["ACTION"] == "remove":
            event_type = MPATH_REMOVED
            valid_paths = None
            path = None
            dm_seqnum = None
        else:
            return None

        return MultipathEvent(event_type, mpath_uuid, path, valid_paths,
                              dm_seqnum)

    def _forward_event(self, event):
        with self._lock:
            monitors = list(self._monitors)

        for m in monitors:
            try:
                m.handle(event)
            except Exception as e:
                self.log.exception("Unhandled exception in %s: %s", m, e)

    def _start_monitors(self, monitors):
        started = []
        try:
            for m in monitors:
                self.log.debug("Starting monitor %s", m)
                m.start()
                started.append(m)
        except:
            self._stop_monitors(started)
            raise

    def _stop_monitors(self, monitors):
        for m in monitors:
            self.log.debug("Stopping monitor %s", m)
            try:
                m.stop()
            except Exception:
                self.log.exception("Unhandled exception stopping %s", m)
