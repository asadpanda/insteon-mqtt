#===========================================================================
#
# Insteon battery powered motion sensor
#
#===========================================================================
import time
from .BatterySensor import BatterySensor
from ..CommandSeq import CommandSeq
from .. import log
from .. import handler
from ..Signal import Signal
from .. import message as Msg
from .. import util

LOG = log.get_logger()


class Motion(BatterySensor):
    """Insteon battery powered motion sensor.

    A motion sensor is an on/off sensor except that it's battery powered and
    only awake when motion is detected or the set button is pressed.

    The issue with a battery powered sensors is that we can't download the
    link database without the sensor being on.  You can trigger the sensor
    manually and then quickly send an MQTT command with the payload 'getdb'
    to download the database.  We also can't test to see if the local
    database is current or what the current motion state is - we can really
    only respond to the sensor when it sends out a message.

    Motion sensors send a Motion.signal_state signal (from BatterySensor)
    when motion is detected.  Some motion sensors also support a dusk/dawn
    light sensor.  In that case, the Motion.signal_dawn signal is emitted
    when the light sensor changes state.

    The device will broadcast messages on the following groups:
      group 01 = on (0x11) / off (0x13)
      group 02 = dusk/dawn light sensor
      group 03 = low battery (0x11) / good battery (0x13)
      group 04 = heartbeat (0x11)

    Note: the newer 2844 model only seems to use group 01.  Dusk dawn does
    not appear to be supported.  Also, the low_battery data is only available
    on request.

    State changes are communicated by emitting signals.  Other classes can
    connect to these signals to perform an action when a change is made to
    the device (like sending MQTT messages).

    - signal_low_battery( Device, bool is_low ): Sent to indicate the current
      battery state.

    - signal_heartbeat( Device, True ): Sent when the device has broadcast a
      heartbeat signal.

    - signal_dawn( Device, bool is_dawn): Sent when the device indicates that
      the light level (dusk/dawn) has changed.  Not all motion sensors support
      this.
    """
    type_name = "motion_sensor"

    # This defines what is the minimum time between battery status requests
    # for devices that support it.  Value is in seconds
    # Currently set at 4 Days
    BATTERY_TIME = (60 * 60) * 24 * 4

    def __init__(self, protocol, modem, address, name=None, config_extra=None):
        """Constructor

        Args:
          protocol (Protocol):  The Protocol object used to communicate
                   with the Insteon network.  This is needed to allow the
                   device to send messages to the PLM modem.
          modem (Modem):  The Insteon modem used to find other devices.
          address (Address): The address of the device.
          name (str):  Nice alias name to use for the device.
          config_extra (dict): Extra configuration settings
        """
        super().__init__(protocol, modem, address, name, config_extra)

        self.signal_dawn = Signal()  # (Device, bool is_dawn)

        # Insert the dawn/dusk callback on group 02.  Base class already
        # handles the other groups.
        self.group_map.update({0x02: self.handle_dawn})

        # Remote (mqtt) commands mapped to methods calls.  Add to the
        # base class defined commands.
        self.cmd_map.update({
            'set_low_battery_voltage': self.set_low_battery_voltage,
            'get_battery_voltage' : self._get_ext_flags,
            })

        # Set default values for bits.  These should always be updated prior
        # to setting
        self.led_on = 1
        self.night_only = 0
        self.on_only = 0

        # This allows for a short timer between sending automatic battery
        # requests.  Otherwise, a request may get queued multiple times
        self._battery_request_time = 0

        # Define the flags handled by set_flags()
        self.set_flags_map.update({"led_on": self.update_flags,
                                   "night_only": self.update_flags,
                                   "on_only": self.update_flags,
                                   "timeout": self._set_timeout,
                                   "light_sensitivity": self._set_light_sens})

    #-----------------------------------------------------------------------
    @property
    def battery_voltage_time(self):
        """Returns the timestamp of the last battery voltage report from the
        saved metadata
        """
        meta = self.db.get_meta('Motion')
        ret = 0
        if isinstance(meta, dict) and 'battery_voltage_time' in meta:
            ret = meta['battery_voltage_time']
        return ret

    #-----------------------------------------------------------------------
    @battery_voltage_time.setter
    def battery_voltage_time(self, val):
        """Saves the timestamp of the last battery voltage report to the
        database metadata
        Args:
          val:    (timestamp) time.time() value
        """
        meta = {'battery_voltage_time': val}
        existing = self.db.get_meta('Motion')
        if isinstance(existing, dict):
            existing.update(meta)
            self.db.set_meta('Motion', existing)
        else:
            self.db.set_meta('Motion', meta)

    #-----------------------------------------------------------------------
    @property
    def battery_low_voltage(self):
        """Returns the voltage below which the battery will be deemed to be
        low.  The default value is 7.0 volts for 2842 models and 1.85 for
        2844 models.
        """
        meta = self.db.get_meta('Motion')
        if (self.db.desc is not None and
                self.db.desc.model.split("-")[0] == "2842"):
            ret = 7.0
        else:
            ret = 1.85

        if isinstance(meta, dict) and 'battery_low_voltage' in meta:
            ret = meta['battery_low_voltage']
        return ret

    #-----------------------------------------------------------------------
    @battery_low_voltage.setter
    def battery_low_voltage(self, val):
        """Saves the voltage below which the battery will be deemed to be
        low.
        Args:
          val:    (float) Low voltage number
        """
        meta = {'battery_low_voltage': val}
        existing = self.db.get_meta('Motion')
        if isinstance(existing, dict):
            existing.update(meta)
            self.db.set_meta('Motion', existing)
        else:
            self.db.set_meta('Motion', meta)

    #-----------------------------------------------------------------------
    def set_low_battery_voltage(self, on_done, voltage=None):
        """Set low voltage value.

        Called from the mqtt command functions or cmd_line

        Args:
          voltage: (float) The low voltage value
          on_done: Finished callback.  This is called when the command has
                   completed.  Signature is: on_done(success, msg, data)
        """
        if voltage is not None:
            LOG.info("Motion %s cmd: set low voltage= %s", self.label, voltage)
            self.battery_low_voltage = voltage
            on_done(True, "Low voltage set.", None)
        else:
            LOG.warning("Motion %s set_low_voltage cmd requires voltage key.",
                        self.label)
            on_done(False, "Low voltage not specified.", None)

    #-----------------------------------------------------------------------
    def handle_dawn(self, msg):
        """Handle a dusk/dawn message.

        This is called by the BatterySensor base class when a group broadcast
        on group 02 is sent out by the sensor.  Not all devices support the
        the light sensor so this may never happen.

        Args:
          msg (InpStandard):  Broadcast message from the device.

        """
        # Send True for dawn, False for dusk.
        LOG.info("Motion %s broadcast grp: %s cmd %s", self.addr,
                 msg.group, msg.cmd1)
        self.signal_dawn.emit(self, msg.cmd1 == Msg.CmdType.ON)

    #-----------------------------------------------------------------------
    def update_flags(self, on_done=None, **kwargs):
        """Change the operating flags.
        """
        seq = CommandSeq(self, "Motion Set Flags Success", on_done,
                         name="UpdateFlags")
        seq.add(self._get_ext_flags)
        seq.add(self._change_flags, kwargs)
        seq.run()

    #-----------------------------------------------------------------------
    def _change_flags(self, flags, on_done=None):
        """Change the operating flags.

        See the set_flags() code for details.
        """
        # Check for valid input
        if 'led_on' in flags:
            led_on = util.input_bool(flags, 'led_on')
            if led_on is None:
                LOG.error("Invalid led on.")
                on_done(False, 'Invalid led on.', None)
                return
        else:
            led_on = self.led_on
        if 'night_only' in flags:
            night_only = util.input_bool(flags, 'night_only')
            if night_only is None:
                LOG.error("Invalid night only.")
                on_done(False, 'Invalid night only.', None)
                return
        else:
            night_only = self.night_only
        if 'on_only' in flags:
            on_only = util.input_bool(flags, 'on_only')
            if on_only is None:
                LOG.error("Invalid on only.")
                on_done(False, 'Invalid on only.', None)
                return
        else:
            on_only = self.on_only

        # Generate the value of the combined flags.
        # on_only and night_only are inverted
        value = 0
        value = util.bit_set(value, 3, led_on)
        value = util.bit_set(value, 2, False if night_only else True)
        value = util.bit_set(value, 1, False if on_only else True)

        # Push the flags value to the device.
        data = bytes([
            0x00,   # D1 = 0x00
            0x05,   # D2 = 0x05 Set Flags
            value,  # D3 = the flag value
            ] + [0x00] * 11)
        msg = Msg.OutExtended.direct(self.addr, 0x2e, 0x00, data)
        callback = self.generic_ack_callback("Flags updated.")
        msg_handler = handler.StandardCmd(msg, callback, on_done)
        self.send(msg, msg_handler)

    #-----------------------------------------------------------------------
    def _get_ext_flags(self, on_done=None):
        """Get the Insteon operational extended flags field from the device.

        For the motion device, these flags include led_on, night_only,
        on_only, as well as the battery voltage and current light level.

        Args:
          on_done: Finished callback.  This is called when the command has
                   completed.  Signature is: on_done(success, msg, data)
        """
        LOG.info("Motion %s cmd: get extended operation flags", self.label)

        # Requesting data is all 0s. Flags are in D6 of ext response msg
        data = bytes([0x00] * 14)

        msg = Msg.OutExtended.direct(self.addr, 0x2e, 0x00, data)
        msg_handler = handler.ExtendedCmdResponse(msg, self.handle_ext_flags,
                                                  on_done)
        self.send(msg, msg_handler)

    #-----------------------------------------------------------------------
    def handle_ext_flags(self, msg, on_done):
        """Handle replies to the _get_ext_flags command.

        Data 6 of the extended response contains the bits for led_on,
        night_only, and on_only.  This parses them out and stores their value
        for use in setting flags.

        Data 11 contains the light level from 0-255.  Not currently used for
        anything.

        Data 12 of the extended response contains the battery voltage /10.

        Args:
          msg (message.InpExtended):  The message reply.  The current
              flags are in D6.
          on_done:  Finished callback.  This is called when the command has
                    completed.  Signature is: on_done(success, msg, data)
        """
        LOG.ui("Motion %s extended operating flags: %s", self.addr,
               "{:08b}".format(msg.data[5]))
        self.led_on = util.bit_get(msg.data[5], 3)
        self.night_only = util.bit_get(msg.data[5], 2)
        self.on_only = util.bit_get(msg.data[5], 1)

        # D11 has the light level, not doing anything with that now.

        # D12 voltage
        if (self.db.desc is not None and
                self.db.desc.model.split("-")[0] == "2842"):
            batt_volt = msg.data[11] / 10
        else:
            # by default assume 2844 model
            batt_volt = round(msg.data[11] / 72, 2)

        LOG.info("Motion %s battery voltage is %s", self.label,
                 batt_volt)
        self.battery_voltage_time = time.time()
        # Signal low battery
        self.signal_low_battery.emit(self,
                                     batt_volt <= self.battery_low_voltage)

        on_done(True, "Operation complete", msg.data[5])

    #-----------------------------------------------------------------------
    def _set_light_sens(self, on_done=None, **kwargs):
        """Change the light sensitivity amount.

        See the set_flags() code for details.
        """
        # Check for valid input
        sensitivity = util.input_byte(kwargs, 'light_sensitivity')
        if sensitivity is None:
            LOG.error("Invalid light sensitivity.")
            on_done(False, 'Invalid light sensitivity.', None)
            return

        # Push the flags value to the device.
        data = bytes([
            0x00,   # D1 = 0x00
            0x04,   # D2 = 0x05 Set Flags
            int(sensitivity),  # D3 = the sensitivity value
            ] + [0x00] * 11)
        msg = Msg.OutExtended.direct(self.addr, 0x2e, 0x00, data)
        callback = self.generic_ack_callback("Light sensitivity updated.")
        msg_handler = handler.StandardCmd(msg, callback, on_done)
        self.send(msg, msg_handler)

    #-----------------------------------------------------------------------
    def _set_timeout(self, on_done=None, **kwargs):
        """Change the timeout in seconds.

        This will automatically change the timeout requested to fit within the
        valid values.

        See the set_flags() code for details.
        """
        # Check for valid input
        timeout = util.input_integer(kwargs, 'timeout')
        if timeout is None:
            LOG.error("Invalid timeout.")
            on_done(False, 'Invalid timeout.', None)
            return

        # The calculation of the timeout value is stored differently on the
        # older 2842 and the newer 2844 motion sensors.  We will assume the
        # newer style as a default.
        if (self.db.desc is not None and
                self.db.desc.model.split("-")[0] == "2842"):
            # Minimum of 30 seconds
            if timeout < 30:
                timeout = 30
            # Max 4 hours
            if timeout > 14400:
                timeout = 14400
            timeout = int(timeout / 30) - 1
            LOG.ui("Motion %s setting timeout to %s seconds", self.addr,
                   ((timeout + 1) * 30))
        else:
            # Assuming this is a 2844 sensor or that is uses the same style
            # Minimum 10 Seconds
            if timeout < 10:
                timeout = 10
            # Max 40 Minutes
            if timeout > 2400:
                timeout = 2400
            timeout = int(timeout / 10)
            LOG.ui("Motion %s setting timeout to %s seconds", self.addr,
                   ((timeout) * 10))

        # Push the flags value to the device.
        data = bytes([
            0x00,   # D1 = 0x00
            0x03,   # D2 = 0x05 Set Flags
            timeout,  # D3 = the sensitivity value
            ] + [0x00] * 11)
        msg = Msg.OutExtended.direct(self.addr, 0x2e, 0x00, data)
        callback = self.generic_ack_callback("Motion timeout updated.")
        msg_handler = handler.StandardCmd(msg, callback, on_done)
        self.send(msg, msg_handler)

    #-----------------------------------------------------------------------
    def auto_check_battery(self):
        """Queues a Battery Voltage Request if Necessary

        If the device supports it, and the requisite amount of time has
        elapsed, queue a battery request.
        """
        if (self.db.desc is not None and
                (self.db.desc.model.split("-")[0] == "2842" or
                 self.db.desc.model.split("-")[0] == "2844")):
            # This is a device that supports battery requests
            last_checked = self.battery_voltage_time
            # Don't send this message more than once every 5 minutes no
            # matter what
            if (last_checked + self.BATTERY_TIME <= time.time() and
                    self._battery_request_time + 300 <= time.time()):
                self._battery_request_time = time.time()
                LOG.info("Motion %s: Auto requesting battery voltage",
                         self.label)
                self._get_ext_flags(None)

    #-----------------------------------------------------------------------
    def awake(self, on_done):
        """Injects a Battery Voltage Request if Necessary

        Queue a battery request that should go out now, since the device is
        awake.
        """
        self.auto_check_battery()
        super().awake(on_done)

    #-----------------------------------------------------------------------
    def _pop_send_queue(self):
        """Injects a Battery Voltage Request if Necessary

        Queue a battery request that should go out now, since the device is
        awake.
        """
        self.auto_check_battery()
        super()._pop_send_queue()

    #-----------------------------------------------------------------------
