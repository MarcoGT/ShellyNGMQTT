# coding=utf-8
import indigo

from ..component import Component


class Cover(Component):
    """
    The Cover component handles a roller/shutter with position control.
    position 0 = closed, 100 = open.
    Mapped to Indigo dimmer: brightness = position, TurnOn = open, TurnOff = close.
    """

    component_type = "cover"
    device_type_id = "component-cover"

    def __init__(self, shelly, device_id, comp_id=0):
        super(Cover, self).__init__(shelly, device_id, comp_id)

    def get_device_state_list(self):
        states = super(Cover, self).get_device_state_list()
        states.extend([
            indigo.activePlugin.getDeviceStateDictForStringType("cover_state", "Cover State", "Cover State"),
            indigo.activePlugin.getDeviceStateDictForNumberType("temperature_c", "Temperature (C)", "Temperature (C)"),
            indigo.activePlugin.getDeviceStateDictForNumberType("temperature_f", "Temperature (F)", "Temperature (F)"),
            indigo.activePlugin.getDeviceStateDictForNumberType("apower", "Power (W)", "Power (W)"),
            indigo.activePlugin.getDeviceStateDictForNumberType("voltage", "Voltage (V)", "Voltage (V)"),
        ])
        return states

    def get_device_display_state_id(self):
        return "cover_state"

    def handle_action(self, action):
        super(Cover, self).handle_action(action)

        if action.deviceAction == indigo.kDeviceAction.TurnOn:
            self.open()
        elif action.deviceAction == indigo.kDeviceAction.TurnOff:
            self.close()
        elif action.deviceAction == indigo.kDeviceAction.Toggle:
            self.stop()
        elif action.deviceAction == indigo.kDeviceAction.SetBrightness:
            self.go_to_position(action.actionValue)
        elif action.deviceAction == indigo.kDeviceAction.BrightenBy:
            new_pos = min(100, self.device.brightness + action.actionValue)
            self.go_to_position(new_pos)
        elif action.deviceAction == indigo.kDeviceAction.DimBy:
            new_pos = max(0, self.device.brightness - action.actionValue)
            self.go_to_position(new_pos)

    def get_status(self):
        self.shelly.publish_rpc("Cover.GetStatus", {'id': self.comp_id}, callback=self.process_status)

    def process_status(self, status, error=None):
        if error:
            self.logger.error(error)
            return

        updated_states = []

        pos = status.get('current_pos', None)
        if pos is not None:
            updated_states.append({'key': 'brightnessLevel', 'value': int(pos)})
            updated_states.append({'key': 'onOffState', 'value': int(pos) > 0})

        cover_state = status.get('state', None)
        if cover_state is not None and "cover_state" in self.device.states:
            updated_states.append({'key': 'cover_state', 'value': cover_state})
            self.log_command_received(cover_state)

        temp_c = status.get('temperature', {}).get('tC', None)
        if temp_c is not None and "temperature_c" in self.device.states:
            updated_states.append({'key': 'temperature_c', 'value': temp_c, 'uiValue': "{} °C".format(temp_c)})
            temp_f = round(temp_c * 9.0 / 5.0 + 32, 1)
            if "temperature_f" in self.device.states:
                updated_states.append({'key': 'temperature_f', 'value': temp_f, 'uiValue': "{} °F".format(temp_f)})

        errors = status.get('errors', None)
        if errors:
            self.device.setErrorStateOnServer(", ".join(errors))
        elif errors is not None:
            self.device.setErrorStateOnServer(None)

        apower = status.get('apower', None)
        if apower is not None and "apower" in self.device.states:
            updated_states.append({'key': 'apower', 'value': apower, 'uiValue': "{} W".format(apower)})

        voltage = status.get('voltage', None)
        if voltage is not None and "voltage" in self.device.states:
            updated_states.append({'key': 'voltage', 'value': voltage, 'uiValue': "{} V".format(voltage)})

        if updated_states:
            self.device.updateStatesOnServer(updated_states)

    def handle_notify_status(self, status):
        self.process_status(status)

    def get_config(self):
        self.shelly.publish_rpc("Cover.GetConfig", {'id': self.comp_id}, callback=self.process_config)

    def process_config(self, config, error=None):
        if error:
            self.logger.error(error)
            return
        self.latest_config = {'name': config.get("name", "")}
        props = self.device.pluginProps
        props.update(self.latest_config)
        self.device.replacePluginPropsOnServer(props)

    def set_config(self, config):
        self.shelly.publish_rpc("Cover.SetConfig", {'id': self.comp_id, 'config': config}, callback=self.process_set_config)

    def process_set_config(self, status, error=None):
        if error:
            self.logger.error("Error writing cover configuration: {}".format(error.get("message", "<Unknown>")))

    def open(self):
        self.shelly.publish_rpc("Cover.Open", {'id': self.comp_id})
        self.log_command_sent("open")

    def close(self):
        self.shelly.publish_rpc("Cover.Close", {'id': self.comp_id})
        self.log_command_sent("close")

    def stop(self):
        self.shelly.publish_rpc("Cover.Stop", {'id': self.comp_id})
        self.log_command_sent("stop")

    def go_to_position(self, pos):
        pos = max(0, min(100, int(pos)))
        self.shelly.publish_rpc("Cover.GoToPosition", {'id': self.comp_id, 'pos': pos})
        self.log_command_sent("go to {}%".format(pos))
