# vim: set et sw=4 sts=4 fileencoding=utf-8:
#
# Raspberry Pi Sense HAT Emulator library for the Raspberry Pi
# Copyright (c) 2016 Raspberry Pi Foundation <info@raspberrypi.org>
#
# This package is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later
# version.
#
# This package is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program. If not, see <http://www.gnu.org/licenses/>

from __future__ import (
    unicode_literals,
    absolute_import,
    print_function,
    division,
    )
nstr = str
str = type('')


import io
import os
import sys
import atexit
import struct
import math
import errno
import subprocess
import webbrowser
from time import time, sleep
from threading import Thread, Lock, Event

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GdkPixbuf, Gio, GLib, GObject
import numpy as np
import pkg_resources

from . import __project__, __version__, __author__, __author_email__, __url__
from .i18n import init_i18n, _
from .screen import ScreenClient
from .imu import IMUServer
from .pressure import PressureServer
from .humidity import HumidityServer
from .stick import StickServer, SenseStick
from .common import HEADER_REC, DATA_REC, DataRecord


def main():
    init_i18n()
    # threads_init isn't required since PyGObject 3.10.2, but just in case
    # we're on something ancient...
    GObject.threads_init()
    app = EmuApplication()
    app.run(sys.argv)


def load_image(filename, format='png'):
    loader = GdkPixbuf.PixbufLoader.new_with_type(format)
    loader.write(pkg_resources.resource_string(__name__, filename))
    loader.close()
    return loader.get_pixbuf()


class EmuApplication(Gtk.Application):
    def __init__(self, *args, **kwargs):
        super(EmuApplication, self).__init__(
                *args, application_id='org.raspberrypi.sense_emu_gui',
                flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
                **kwargs)
        GLib.set_application_name(_('Sense HAT Emulator'))
        self.window = None

    def do_startup(self):
        # super-call needs to be in this form?!
        Gtk.Application.do_startup(self)

        def make_action(action_id, handler, param_type=None):
            action = Gio.SimpleAction.new(action_id, param_type)
            action.connect('activate', handler)
            self.add_action(action)
        make_action('example', self.on_example, GLib.VariantType.new('s'))
        make_action('play',    self.on_play)
        make_action('prefs',   self.on_prefs)
        make_action('help',    self.on_help)
        make_action('about',   self.on_about)
        make_action('quit',    self.on_quit)

        builder = Gtk.Builder(translation_domain=__project__)
        builder.add_from_string(
            pkg_resources.resource_string(__name__, 'menu.ui').decode('utf-8'))
        self.props.menubar = builder.get_object('app-menu')

        # Construct the open examples sub-menu
        examples = Gio.Menu.new()
        for example in sorted(pkg_resources.resource_listdir(__name__, 'examples')):
            if example.endswith('.py'):
                examples.append(
                    example.replace('_', '__'), Gio.Action.print_detailed_name(
                        'app.example', GLib.Variant.new_string(example)))
        builder.get_object('example-submenu').append_section(None, examples)

        # Construct the emulator servers
        self.imu = IMUServer()
        self.pressure = PressureServer()
        self.humidity = HumidityServer()
        self.screen = ScreenClient()
        self.stick = StickServer()

    def do_shutdown(self):
        if self.window:
            self.window.destroy()
            self.window = None
        self.stick.close()
        self.screen.close()
        self.humidity.close()
        self.pressure.close()
        self.imu.close()
        Gtk.Application.do_shutdown(self)

    def do_activate(self):
        if not self.window:
            self.window = MainWindow(application=self)
        self.window.present()

    def do_command_line(self, command_line):
        options = command_line.get_options_dict()
        # do stuff with switches
        self.activate()
        return 0

    def on_help(self, action, param):
        local_help = '/usr/share/doc/python-sense-emu-doc/html/index.html'
        remote_help = 'https://sense-emu.readthedocs.io/'
        if os.path.exists(local_help):
            webbrowser.open('file://' + local_help)
        else:
            webbrowser.open(remote_help)

    def on_about(self, action, param):
        logo = load_image('sense_emu_gui.svg', format='svg')
        about_dialog = Gtk.AboutDialog(
            transient_for=self.window,
            authors=['%s <%s>' % (__author__, __author_email__)],
            license_type=Gtk.License.GPL_2_0, logo=logo,
            version=__version__, website=__url__)
        about_dialog.run()
        about_dialog.destroy()

    def on_example(self, action, param):
        def already_exists(target_filename):
            # This sub-function is purely here to eliminate the redundancy in
            # the main try..except block below (and the redundancy only exists
            # to maintain compatibility with 2.x)
            dialog = Gtk.MessageDialog(
                transient_for=self.window,
                message_type=Gtk.MessageType.QUESTION,
                title=_('Overwrite example?'),
                text=_(
                    'File %s already exists. Select "Yes" to replace '
                    'the file with the original, or "No" to open the '
                    'existing file in the editor') % target_filename,
                buttons=Gtk.ButtonsType.YES_NO)
            try:
                response = dialog.run()
                if response == Gtk.ResponseType.YES:
                    return io.open(target_filename, 'wb')
                else:
                    return None
            finally:
                dialog.destroy()

        filename = param.unpack()
        target_filename = os.path.join(os.path.expanduser('~'), filename)
        try:
            target = io.open(target_filename, 'xb')
        except ValueError as e:
            # We're on py2.x or 3.2 which doesn't support mode 'x'
            try:
                target = os.fdopen(os.open(
                    target_filename, os.O_CREAT | os.O_EXCL | os.O_WRONLY), 'wb')
            except OSError as e:
                if e.errno == errno.EEXIST:
                    target = already_exists(target_filename)
        except IOError as e:
            if e.errno == errno.EEXIST:
                target = already_exists(target_filename)
        if target:
            # NOTE: The use of a bare "/" below is correct: resource paths are
            # *not* file-system paths and always use "/" path separators
            source = pkg_resources.resource_stream(
                __name__, '/'.join(('examples', filename)))
            target.write(source.read())
            source.close()
            target.close()
        subprocess.Popen(['idle3', target_filename])

    def on_play(self, action, param):
        open_dialog = Gtk.FileChooserDialog(
            transient_for=self.window,
            title=_('Select the recording to play'),
            action=Gtk.FileChooserAction.OPEN)
        open_dialog.add_button(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL)
        open_dialog.add_button(Gtk.STOCK_OPEN, Gtk.ResponseType.ACCEPT)
        try:
            response = open_dialog.run()
            open_dialog.hide()
            if response == Gtk.ResponseType.ACCEPT:
                self.window.play(open_dialog.get_filename())
        finally:
            open_dialog.destroy()

    def on_prefs(self, action, param):
        prefs_dialog = PrefsDialog(
            transient_for=self.window,
            title=_('Preferences'))
        try:
            prefs_dialog.ui.env_check.props.active = (
                self.pressure.simulate_noise or self.humidity.simulate_noise)
            prefs_dialog.ui.imu_check.props.active = self.imu.simulate_world
            prefs_dialog.ui.screen_fps.props.value = 1 / self.window.screen_update_delay
            prefs_dialog.ui.orientation_balance.props.active = self.window.orientation_mode == 'balance'
            prefs_dialog.ui.orientation_circle.props.active = self.window.orientation_mode == 'circle'
            prefs_dialog.ui.orientation_modulo.props.active = self.window.orientation_mode == 'modulo'
            response = prefs_dialog.run()
            prefs_dialog.hide()
            if response == Gtk.ResponseType.ACCEPT:
                self.pressure.simulate_noise = prefs_dialog.ui.env_check.props.active
                self.humidity.simulate_noise = prefs_dialog.ui.env_check.props.active
                self.imu.simulate_world = prefs_dialog.ui.imu_check.props.active
                self.window.screen_update_delay = 1 / prefs_dialog.ui.screen_fps.props.value
                self.window.orientation_mode = (
                    'balance' if prefs_dialog.ui.orientation_balance.props.active else
                    'circle' if prefs_dialog.ui.orientation_circle.props.active else
                    'modulo')
                # Force the orientation sliders to redraw (merely changing
                # format isn't enough to force a redraw)
                self.window.ui.yaw.props.value = self.window.ui.yaw.props.value
                self.window.ui.pitch.props.value = self.window.ui.pitch.props.value
                self.window.ui.roll.props.value = self.window.ui.roll.props.value
        finally:
            prefs_dialog.destroy()

    def on_quit(self, action, param):
        self.quit()


class BuilderUi(object):
    def __init__(self, owner, filename):
        # Load the GUI definitions (see __getattr__ for how we tie the loaded
        # objects into instance variables) and connect all handlers to methods
        # on this object
        self._builder = Gtk.Builder(translation_domain=__project__)
        self._builder.add_from_string(
            pkg_resources.resource_string(__name__, filename).decode('utf-8'))
        self._builder.connect_signals(owner)

    def __getattr__(self, name):
        result = self._builder.get_object(name)
        if result is None:
            raise AttributeError(_('No such attribute %r') % name)
        setattr(self, name, result)
        return result


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, *args, **kwargs):
        super(MainWindow, self).__init__(*args, **kwargs)

        # Build the UI; this is a bit round-about because of Gtk's weird UI
        # handling. One can't just use a UI file to setup an existing Window
        # instance (as in Qt); instead one must use a separate handler object
        # (in which case overriding do_destroy is impossible) or construct a
        # whole new Window, remove its components, add them to ourselves and
        # then ditch the Window.
        self._ui = BuilderUi(self, 'main_window.ui')
        self.ui.window.remove(self.ui.root_grid)
        self.add(self.ui.root_grid)
        self.ui.window.destroy()

        # Set up the objects for the playback thread
        self._play_update_lock = Lock()
        self._play_update_id = 0
        self._play_event = Event()
        self._play_thread = None
        self._play_restore = (True, True, True)

        # Set initial positions on sliders (and add some marks)
        self.orientation_mode = 'balance'
        self.ui.pitch_scale.add_mark(0, Gtk.PositionType.BOTTOM, None)
        self.ui.roll_scale.add_mark(0, Gtk.PositionType.BOTTOM, None)
        self.ui.yaw_scale.add_mark(0, Gtk.PositionType.BOTTOM, None)
        self.ui.pitch.props.value = self.props.application.imu.orientation[0]
        self.ui.roll.props.value = self.props.application.imu.orientation[1]
        self.ui.yaw.props.value = self.props.application.imu.orientation[2]
        self.ui.humidity.props.value = self.props.application.humidity.humidity
        self.ui.pressure.props.value = self.props.application.pressure.pressure
        self.ui.temperature.props.value = self.props.application.humidity.temperature

        # Load graphics assets
        self.sense_image = load_image('sense_emu.png')
        self.pixel_grid = load_image('pixel_grid.png')
        self.ui.yaw_image.set_from_pixbuf(load_image('yaw.png'))
        self.ui.pitch_image.set_from_pixbuf(load_image('pitch.png'))
        self.ui.roll_image.set_from_pixbuf(load_image('roll.png'))

        # Set up attributes for the joystick buttons
        self._stick_held_lock = Lock()
        self._stick_held_id = 0
        self.ui.left_button.direction = SenseStick.KEY_LEFT
        self.ui.right_button.direction = SenseStick.KEY_RIGHT
        self.ui.up_button.direction = SenseStick.KEY_UP
        self.ui.down_button.direction = SenseStick.KEY_DOWN
        self.ui.enter_button.direction = SenseStick.KEY_ENTER
        self._stick_map = {
            Gdk.KEY_Return: self.ui.enter_button,
            Gdk.KEY_Left:   self.ui.left_button,
            Gdk.KEY_Right:  self.ui.right_button,
            Gdk.KEY_Up:     self.ui.up_button,
            Gdk.KEY_Down:   self.ui.down_button,
            }

        # Set up attributes for the screen rotation controls
        self.ui.screen_rotate_clockwise.angle = -90
        self.ui.screen_rotate_anticlockwise.angle = 90
        self._stick_rotations = {
            SenseStick.KEY_LEFT:  SenseStick.KEY_UP,
            SenseStick.KEY_UP:    SenseStick.KEY_RIGHT,
            SenseStick.KEY_RIGHT: SenseStick.KEY_DOWN,
            SenseStick.KEY_DOWN:  SenseStick.KEY_LEFT,
            SenseStick.KEY_ENTER: SenseStick.KEY_ENTER,
            }

        # Set up a thread to constantly refresh the pixels from the screen
        # client object
        self.screen_update_delay = 0.04
        self._screen_rotation = 0
        self._screen_pending = False
        self._screen_timestamp = 0.0
        self._screen_event = Event()
        self._screen_thread = Thread(target=self._screen_run)
        self._screen_thread.daemon = True
        self._screen_thread.start()

    @property
    def ui(self):
        return self._ui

    def do_destroy(self):
        try:
            self._play_stop()
            self._screen_event.set()
            self._screen_thread.join()
        except AttributeError:
            # do_destroy gets called multiple times, and subsequent times lacks
            # the Python-added instance attributes
            pass
        Gtk.ApplicationWindow.do_destroy(self)

    def format_pressure(self, scale, value):
        return '%.1fmbar' % value

    def pressure_changed(self, adjustment):
        if not self._play_thread:
            self.props.application.pressure.set_values(
                self.ui.pressure.props.value,
                self.ui.temperature.props.value,
                )

    def format_humidity(self, scale, value):
        return '%.1f%%' % value

    def humidity_changed(self, adjustment):
        if not self._play_thread:
            self.props.application.humidity.set_values(
                self.ui.humidity.props.value,
                self.ui.temperature.props.value,
                )

    def format_temperature(self, scale, value):
        return '%.1f°C' % value

    def temperature_changed(self, adjustment):
        if not self._play_thread:
            self.pressure_changed(adjustment)
            self.humidity_changed(adjustment)

    def format_orientation(self, scale, value):
        return '%.1f°' % (
            value       if self.orientation_mode == 'balance' else
            value + 180 if self.orientation_mode == 'circle' else
            value % 360 if self.orientation_mode == 'modulo' else
            999 # should never happen
            )

    def orientation_changed(self, adjustment):
        if not self._play_thread:
            self.props.application.imu.set_orientation((
                self.ui.pitch.props.value,
                self.ui.roll.props.value,
                self.ui.yaw.props.value,
                ))

    def stick_key_pressed(self, button, event):
        try:
            button = self._stick_map[event.keyval]
        except KeyError:
            return False
        else:
            self.stick_pressed(button, event)
            return True

    def stick_key_released(self, button, event):
        try:
            button = self._stick_map[event.keyval]
        except KeyError:
            return False
        else:
            self.stick_released(button, event)
            return True

    def stick_pressed(self, button, event):
        # When a button is double-clicked, GTK fires two pressed events for the
        # second click with no intervening released event (so there's one
        # pressed event for the first click, followed by a released event, then
        # two pressed events for the second click followed by a single released
        # event). This isn't documented, so it could be a bug, but it seems
        # more like a deliberate behaviour. Anyway, we work around the
        # redundant press by detecting it with the non-zero stick_held_id and
        # ignoring the redundant event
        button.grab_focus()
        with self._stick_held_lock:
            if self._stick_held_id:
                return True
            self._stick_held_id = GLib.timeout_add(250, self.stick_held_first, button)
        self._stick_send(button.direction, SenseStick.STATE_PRESS)
        button.set_active(True)
        return True

    def stick_released(self, button, event):
        with self._stick_held_lock:
            if self._stick_held_id:
                GLib.source_remove(self._stick_held_id)
                self._stick_held_id = 0
        self._stick_send(button.direction, SenseStick.STATE_RELEASE)
        button.set_active(False)
        return True

    def stick_held_first(self, button):
        with self._stick_held_lock:
            self._stick_held_id = GLib.timeout_add(50, self.stick_held, button)
        self._stick_send(button.direction, SenseStick.STATE_HOLD)
        return False

    def stick_held(self, button):
        self._stick_send(button.direction, SenseStick.STATE_HOLD)
        return True

    def _stick_send(self, direction, action):
        tv_usec, tv_sec = math.modf(time())
        tv_usec *= 1000000
        r = self._screen_rotation // 90
        while r:
            direction = self._stick_rotations[direction]
            r -= 1
        event_rec = struct.pack(SenseStick.EVENT_FORMAT,
            int(tv_sec), int(tv_usec), SenseStick.EV_KEY, direction, action)
        self.props.application.stick.send(event_rec)

    def rotate_screen(self, button):
        self._screen_rotation = (self._screen_rotation + button.angle) % 360
        self.ui.screen_rotate_label.props.label = '%d°' % self._screen_rotation
        self._screen_timestamp = 0 # force a screen update

    def _screen_run(self):
        # This method runs in the background _screen_thread
        while True:
            # Only update the screen if there's no idle callback pending from
            # a prior update
            if not self._screen_pending:
                # Only update if the screen's modification timestamp indicates
                # that the data has changed since last time
                ts = self.props.application.screen.timestamp
                if ts > self._screen_timestamp:
                    img = self.sense_image.copy()
                    pixels = GdkPixbuf.Pixbuf.new_from_bytes(
                        GLib.Bytes.new(self.props.application.screen.rgb_array.tostring()),
                        colorspace=GdkPixbuf.Colorspace.RGB, has_alpha=False,
                        bits_per_sample=8, width=8, height=8, rowstride=8 * 3)
                    pixels.composite(img, 31, 38, 128, 128, 31, 38, 16, 16,
                        GdkPixbuf.InterpType.NEAREST, 255)
                    self.pixel_grid.composite(img, 31, 38, 128, 128, 31, 38, 1, 1,
                        GdkPixbuf.InterpType.NEAREST, 255)
                    img = img.rotate_simple(self._screen_rotation)
                    self._screen_pending = True
                    self._screen_timestamp = ts
                    # GTK updates must be done by the main thread; schedule
                    # the image to be redrawn when the app is idle. It would
                    # be better to use custom signals for this ... but then
                    # pixman region copy bugs start appearing
                    GLib.idle_add(self._update_screen, img)
            # The following wait enforces the maximum update rate (set from
            # the preferences dialog)
            if self._screen_event.wait(self.screen_update_delay):
                break

    def _update_screen(self, pixbuf):
        self.ui.screen_image.set_from_pixbuf(pixbuf)
        self._screen_pending = False
        return False

    def _play_run(self, f):
        err = None
        try:
            # Calculate how many records are in the file; we'll use this later
            # when updating the progress bar
            rec_total = (f.seek(0, io.SEEK_END) - HEADER_REC.size) // DATA_REC.size
            f.seek(0)
            skipped = 0
            for rec, data in enumerate(self._play_source(f)):
                now = time()
                if data.timestamp < now:
                    skipped += 1
                    continue
                else:
                    if self._play_event.wait(data.timestamp - now):
                        break
                self.props.application.pressure.set_values(data.pressure, data.ptemp)
                self.props.application.humidity.set_values(data.humidity, data.htemp)
                self.props.application.imu.set_imu_values(
                    (data.ax, data.ay, data.az),
                    (data.gx, data.gy, data.gz),
                    (data.cx, data.cy, data.cz),
                    (data.ox, data.oy, data.oz),
                    )
                # Again, would be better to use custom signals here but
                # attempting to do so just results in seemingly random
                # segfaults during playback
                with self._play_update_lock:
                    if self._play_update_id == 0:
                        self._play_update_id = GLib.idle_add(self._play_update_controls, rec / rec_total)
        except Exception as e:
            err = e
        finally:
            f.close()
            # Must ensure that controls are only re-enabled *after* all pending
            # control updates have run
            with self._play_update_lock:
                if self._play_update_id:
                    GLib.source_remove(self._play_update_id)
                    self._play_update_id = 0
            # Get the main thread to re-enable the controls at the end of
            # playback
            GLib.idle_add(self._play_controls_finish, err)

    def _play_update_controls(self, fraction):
        with self._play_update_lock:
            self._play_update_id = 0
        self.ui.play_progressbar.props.fraction = fraction
        if not math.isnan(self.props.application.humidity.temperature):
            self.ui.temperature.props.value = self.props.application.humidity.temperature
        if not math.isnan(self.props.application.pressure.pressure):
            self.ui.pressure.props.value = self.props.application.pressure.pressure
        if not math.isnan(self.props.application.humidity.humidity):
            self.ui.humidity.props.value = self.props.application.humidity.humidity
        self.ui.yaw.props.value = math.degrees(self.props.application.imu.orientation[2])
        self.ui.pitch.props.value = math.degrees(self.props.application.imu.orientation[1])
        self.ui.roll.props.value = math.degrees(self.props.application.imu.orientation[0])
        return False

    def play_stop_clicked(self, button):
        self._play_stop()

    def _play_stop(self):
        if self._play_thread:
            self._play_event.set()
            self._play_thread.join()
            self._play_thread = None

    def _play_source(self, f):
        magic, ver, offset = HEADER_REC.unpack(f.read(HEADER_REC.size))
        if magic != b'SENSEHAT':
            raise IOError(_('%s is not a Sense HAT recording') % f.name)
        if ver != 1:
            raise IOError(_('%s has unrecognized file version number') % f.name)
        offset = time() - offset
        while True:
            buf = f.read(DATA_REC.size)
            if not buf:
                break
            elif len(buf) < DATA_REC.size:
                raise IOError(_('Incomplete data record at end of %s') % f.name)
            else:
                data = DataRecord(*DATA_REC.unpack(buf))
                yield data._replace(timestamp=data.timestamp + offset)

    def _play_controls_setup(self, filename):
        # Disable all the associated user controls while playing back
        self.ui.environ_box.props.sensitive = False
        self.ui.gyro_grid.props.sensitive = False
        # Disable simulation threads as we're going to manipulate the
        # values precisely
        self._play_restore = (
            self.props.application.pressure.simulate_noise,
            self.props.application.humidity.simulate_noise,
            self.props.application.imu.simulate_world,
            )
        self.props.application.pressure.simulate_noise = False
        self.props.application.humidity.simulate_noise = False
        self.props.application.imu.simulate_world = False
        # Show the playback bar
        self.ui.play_label.props.label = _('Playing %s') % os.path.basename(filename)
        self.ui.play_progressbar.props.fraction = 0.0
        self.ui.play_box.props.visible = True

    def _play_controls_finish(self, exc):
        # Reverse _play_controls_setup
        self.ui.play_box.props.visible = False
        ( self.props.application.pressure.simulate_noise,
            self.props.application.humidity.simulate_noise,
            self.props.application.imu.simulate_world,
            ) = self._play_restore
        self.ui.environ_box.props.sensitive = True
        self.ui.gyro_grid.props.sensitive = True
        self._play_thread = None
        # If an exception occurred in the background thread, display the
        # error in an appropriate dialog
        if exc:
            dialog = Gtk.MessageDialog(
                transient_for=self,
                message_type=Gtk.MessageType.ERROR,
                title=_('Error'),
                text=_('Error while replaying recording'),
                buttons=Gtk.ButtonsType.CLOSE)
            dialog.format_secondary_text(str(exc))
            dialog.run()
            dialog.destroy()

    def play(self, filename):
        self._play_stop()
        self._play_controls_setup(filename)
        self._play_thread = Thread(target=self._play_run, args=(io.open(filename, 'rb'),))
        self._play_event.clear()
        self._play_thread.start()


class PrefsDialog(Gtk.Dialog):
    def __init__(self, *args, **kwargs):
        super(PrefsDialog, self).__init__(*args, **kwargs)

        # See comments in MainWindow...
        self._ui = BuilderUi(self, 'prefs_dialog.ui')
        self.ui.window.remove(self.ui.dialog_vbox)
        self.remove(self.get_content_area())
        self.add(self.ui.dialog_vbox)
        self.ui.window.destroy()

        self.props.resizable = False
        self.ui.ok_button.grab_default()

    @property
    def ui(self):
        return self._ui

    def ok_clicked(self, button):
        self.response(Gtk.ResponseType.ACCEPT)

    def cancel_clicked(self, button):
        self.response(Gtk.ResponseType.CANCEL)

