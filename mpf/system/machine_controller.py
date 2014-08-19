"""The main machine object for the Mission Pinball Framework."""
# devices.py (contains classes for various playfield devices)
# Mission Pinball Framework
# Written by Brian Madden & Gabe Knuth
# Released under the MIT License. (See license info at the end of this file.)

# Documentation and more info at http://missionpinball.com/framework

import logging
import os
import yaml
from collections import defaultdict
from copy import deepcopy
import time

from mpf.system.hardware import DeviceCollection
from mpf.system.timing import Timing, Timer
from mpf.system.tasks import Task, DelayManager
from mpf.system.events import EventManager
from mpf.system.switch_controller import SwitchController
from mpf.system.ball_controller import BallController
from mpf.modules.ball_search import BallSearch
from mpf.modules.keyboard import Keyboard
from mpf.devices.autofire import AutofireCoil
from mpf.devices.ball_device import BallDevice
from mpf.devices.driver import Driver
from mpf.devices.flipper import Flipper
from mpf.devices.switch import Switch
from mpf.system.shot_controller import ShotController
from mpf.system.score_controller import ScoreController
from mpf.devices.score_reel import (ScoreReel, ScoreReelGroup,
                                    ScoreReelController)


class MachineController(object):
    """Base class for the Machine Controller object.

    The machine controller is the main entity of the entire framework. It's the
    main part that's in charge and makes things happen.

    Parameters
    ----------

    config_file : str
        The name of the main configuration file which will be read when the
        machine controller is created. This config file typically contains a
        list of additional config files that hold settings for the machine, but
        it can also hold settings itself.

    physical_hw : bool
        Specifies whether there is a physical pinball controller attached, or
        whether the machine controller should operate in software-only
        (virtual hardware) mode.
    """

    def __init__(self, config_file, physical_hw=True):
        self.log = logging.getLogger("Machine Controller")
        self.starttime = time.clock()
        self.config = defaultdict(int)  # so we can simplify checking
        self.physical_hw = physical_hw
        self.switch_events = []
        self.done = False
        self.HZ = None
        self.gameflow_index = 0

        # todo make these conditional so they only load if they're used.
        self.coils = DeviceCollection()
        self.lamps = DeviceCollection()
        self.switches = DeviceCollection()
        self.leds = DeviceCollection()
        self.autofires = DeviceCollection()
        self.flippers = DeviceCollection()
        self.balldevices = DeviceCollection()
        self.score_reels = DeviceCollection()
        self.score_reel_groups = DeviceCollection()
        # todo combine leds and lamps?
        # todo add GI and flashers?

        # create all the machine-wide objects & set them up.
        self.events = EventManager()
        self.update_config(config_file)
        self.platform = self.set_platform()
        self.timing = Timing(self)
        self.timing.configure(HZ=self.config['Machine']['HZ'])
        self.switch_controller = SwitchController(self)
        self.process_config()

        self.ball_controller = BallController(self)

        # Optional components which should only load if they're used
        # todo make these conditional

        self.ballsearch = BallSearch(self)
        self.shots = ShotController(self)
        self.scoring = ScoreController(self)
        if 'Score Reel Groups' in self.config:
            self.score_reel_controller = ScoreReelController(self)

        # todo clean this up when you fix game flow
        self.game = None
        self.attract = None

        self.events.post("machine_init_complete")
        self.reset()

        # register event handlers
        self.events.add_handler('machine_flow_advance', self.flow_advance)

    def reset(self):
        """Resets the machine."""
        # Do we want to reset all timers here? todo
        # do we post an event when we do this? Really this should re-read
        # the config and stuff, right? Maybe we destroy all of our objects
        # even and recreate them?

        # reset variables
        self.gameflow_index = 0

        #self.periodic = self.timing.add(Timer(self.periodic_timer_test,
        #                                      frequency=2000))

        self.switch_controller.add_switch_handler(switch_name='start', state=1,
                                                  ms=500, callback=self.test)
        #self.switch_controller.add_switch_handler('start', self.test, 0, 4000)

        # todo now start attract mode
        # self.attract_mode = Task()

        # after our reset is over, we start the gameflow
        self._flow_do()

    def flow_advance(self, position=None):
        """Advances the machine to the next machine mode as specified in the
        gameflow. Typically this just advances between Attract mode and Game
        mode.

        """

        if not position:
            if self.gameflow_index == len(self.config['GameFlow']) - 1:
                self.gameflow_index = 0
            else:
                self.gameflow_index += 1
        else:
            self.gameflow_index = position
        self.log.debug("Advancing Machine Flow. New Index: %s",
                       self.gameflow_index)
        self._flow_do()

    def _flow_do(self):
        # todo change this to set it self up automatically from the yaml file
        # Maybe this can switch to events, where there's an event like
        # 'gameflow_<yamlentry>' that each of these things watches for?

        from mpf.game.attract import Attract
        from mpf.game.game import Game

        if self.gameflow_index == 0:
            self.attract = Attract(self)
            if self.game:
                self.game.stop()
                self.game = None
            self.attract.start()
        elif self.gameflow_index == 1:
            self.game = Game(self)
            if self.attract:
                self.attract.stop()
                self.attract = None
            self.game.start()

        # todo when I fix this, also clean up the stuff in init

    def update_config(self, config):
        """Merges updates into the self.config dictionary.

        This method does what we call a "deep merge" which means it merges
        together subdictionaries instead of overwriting them. See the
        documentation for `meth:dict_merge` for a description of how this
        works.

        Parameters
        ----------

        config : dict or str
            The settings to deep merge into the config dictionary. If `config`
            is a dict, then it will merge those settings in. If it's a string,
            it will try to find a file with that name and open it to read in
            the settings.

            Also, if config is a string, it will first try to open it as a file
            directly (including any path that's there). If that doesn't work,
            it will try to open the file using the last path that worked. (This
            path is stored in `self.config['Config_path']`.)

        """
        new_updates = dict()
        if not self.config['Config_path']:
            self.config['Config_path'] = ""

        if type(config) == dict:
            new_updates = config
        else:  # Maybe 'config' is a file?
            if os.path.isfile(config):
                config_location = config
                # Pull out the path in case we need it later
                self.config['Config_path'] = os.path.split(config)[0]
            elif os.path.isfile(os.path.join(self.config['Config_path'],
                                             config)):
                config_location = os.path.join(self.config['Config_path'],
                                               config)
            else:
                self.log.warning("Couldn't find config file: %s. Skipping.",
                                 config)
                config_location = ""

        if config_location:
            try:
                self.log.debug("Loading configuration from file: %s",
                               config_location)
                new_updates = yaml.load(open(config_location, 'r'))
            except yaml.YAMLError, exc:
                if hasattr(exc, 'problem_mark'):
                    mark = exc.problem_mark
                    self.log.error("Error found in config file %s. Line %, "
                                   "Position %s", config_location, mark.line+1,
                                   mark.column+1)
            except:
                self.log.warning("Couldn't load config from file: %s", config)

        self.config = self.dict_merge(self.config, new_updates)

        # now check if there are any more updates to do.
        # iterate and remove them
        if self.config['Config']:
            if config in self.config['Config']:
                self.config['Config'].remove(config)
            if self.config['Config']:
                self.update_config(self.config['Config'][0])

    def set_platform(self):
        """ Sets the hardware platform based on the "Platform" item in the
        configuration dictionary. Looks for a module of that name in the
        /platform directory.

        """

        try:
            hardware_platform = __import__('mpf.platform.%s' %
                               self.config['Hardware']['Platform'],
                               fromlist=["HardwarePlatform"])
            # above line has an effect similar to:
            # from mpf.platform.<platform_name> import HardwarePlatform
            return hardware_platform.HardwarePlatform(self)

        except ImportError:
            self.log.error("Error importing platform module: %s",
                           self.config['Hardware']['Platform'])
            quit()  # No point in continuing if we error here

    def process_config(self):
        """Processes the hardware config based on the machine config files.

        """
        #self.platform.process_hw_config()  # This just sets the low level hw

        # read in op settings and merge them into the config dictionary

        # Now setup all the devices:
        # todo also could probably automate this instead of having these very
        # similar repeating blocks

        # Coils
        if 'Coils' in self.config:
            for coil in self.config['Coils']:
                # Driver init (machine, name, config)
                Driver(self, coil, self.config['Coils'][coil], self.coils)

        # Switches
        if 'Switches' in self.config:
            for switch in self.config['Switches']:
                # Driver init (machine, name, config)
                Switch(self, switch, self.config['Switches'][switch],
                       self.switches)

        # Flippers
        if 'Flippers' in self.config:
            for flipper in self.config['Flippers']:
                Flipper(self, flipper,
                        self.config['Flippers'][flipper],
                        self.flippers)

        # Autofire Coils
        if 'Autofire Coils' in self.config:
            for coil in self.config['Autofire Coils']:
                AutofireCoil(self, coil,
                             self.config['Autofire Coils'][coil],
                             self.autofires)

        # Ball Devices
        if 'BallDevices' in self.config:
            for balldevice in self.config['BallDevices']:
                BallDevice(self, balldevice,
                           self.config['BallDevices'][balldevice],
                           self.balldevices)

        # Keyboard mapping
        if 'Keyboard' in self.config:
            self.keyboard = Keyboard(self)

        # Score Reel
        if 'Score Reels' in self.config:
            for score_reel in self.config['Score Reels']:
                ScoreReel(self, score_reel,
                          self.config['Score Reels'][score_reel],
                          self.score_reels)

        # Score Reel Groups
        if 'Score Reel Groups' in self.config:
            for reel_group in self.config['Score Reel Groups']:
                ScoreReelGroup(self, reel_group,
                               self.config['Score Reel Groups'][
                               reel_group], self.score_reel_groups)

    def string_to_list(self, string):
        """ Converts a comma-separated string into a python list.

        Parameters
        ----------

        string : str
            The string you'd like to convert.

        Returns
        -------
            A python list object containing whatever was between commas in the
            string.

        """

        # make this a static method

        if type(string) is str:
            # convert to list then strip out leading / trailing white space
            return [x.strip() for x in string.split(',')]
        else:
            # if we're not passed a string, just return an empty list.
            return []

    def dict_merge(self, a, b, combine_lists=True):
        """Recursively merges dicts.

        Used to merge dictionaries of dictionaries, like when we're merging
        together the machine configuration files. This method is called
        recursively as it finds sub-dictionaries.

        For example, in the traditional python dictionary
        update() methods, if a dictionary key exists in the original and
        merging-in dictionary, the new value will overwrite the old value.

        Consider the following example:

        Original dictionary:
        `config['foo']['bar'] = 1`

        New dictionary we're merging in:
        `config['foo']['other_bar'] = 2`

        Default python dictionary update() method would have the updated
        dictionary as this:

        `{'foo': {'other_bar': 2}}`

        This happens because the original dictionary which had the single key
        `bar` was overwritten by a new dictionary which has a single key
        `other_bar`.)

        But really we want this:

        `{'foo': {'bar': 1, 'other_bar': 2}}`

        This code was based on this:
        https://www.xormedia.com/recursively-merge-dictionaries-in-python/

        Parameters
        ----------

        a : dict
            The first dictionary

        b : dict
            The second dictionary

        combine_lists : bool
            Controls whether lists should be combined (extended) or
            overwritten. Default is `True` which combines them.

        Returns
        -------

        The merged dictionaries.

        """
        if not isinstance(b, dict):
            return b
        result = deepcopy(a)
        for k, v in b.iteritems():
            if k in result and isinstance(result[k], dict):
                result[k] = self.dict_merge(result[k], v)
            elif k in result and isinstance(result[k], list) and combine_lists:
                result[k].extend(v)
            else:
                result[k] = deepcopy(v)
        return result

    def enable_autofires(self):
        """Enables all the autofire coils in the machine."""

        self.log.debug("Enabling autofire coils")
        for autofire in self.autofires:
            autofire.enable()

    def disable_autofires(self):
        """Disables all the autofire coils in the machine."""

        self.log.debug("Disabling autofire coils")
        for autofire in self.autofires:
            autofire.disable()

    def enable_flippers(self):
        """Enables all the flippers in the machine."""

        self.log.debug("Enabling flippers")
        for flipper in self.flippers:
            flipper.enable()

    def disable_flippers(self):
        """Disables all the flippers in the machine."""

        self.log.debug("Disabling flippers")
        for flipper in self.flippers:
            flipper.disable()

    def run(self):
        """The main machine run loop.

        """
        self.log.debug("Starting the main machine run loop.")
        num_loops = 0

        self.platform.timer_initialize()

        if self.platform.features['hw_polling']:
            loop = self.platform.hw_loop
        else:
            loop = self.sw_loop

        while self.done is False:
            loop()
            num_loops += 1

        else:
            if num_loops != 0:
                self.log.info("Hardware loop speed: %sHz",
                               round(num_loops /
                                     (time.clock() - self.starttime)))

        # todo add support to read software switch events

    def sw_loop(self):
        """ This is the main game run loop that's used when the hardware
        platform doesn't have to be polled continuously.

        """
        # todo currently this just runs as fast as it can. Should I have it
        # sleep while waiting for the next timer tick?

        if self.platform.next_tick_time <= time.clock():
            self.timer_tick()
            self.platform.next_tick_time += Timing.secs_per_tick

    def timer_tick(self):
        """Called by the platform each machine tick based on self.HZ"""
        self.timing.timer_tick()  # notifies the timing module
        self.events.post('timer_tick')  # sends the timer_tick system event
        Task.timer_tick()  # notifies tasks
        DelayManager.timer_tick()

    def test(self):
        print "in test method."

    def end_run_loop(self):
        """Causes the main run_loop to end."""
        self.done = True

# The MIT License (MIT)

# Copyright (c) 2013-2014 Brian Madden and Gabe Knuth

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
