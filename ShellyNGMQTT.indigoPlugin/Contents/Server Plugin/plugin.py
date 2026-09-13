import indigo
import logging
import re

from queue import Queue

from shelly.devices.Shelly import Shelly
from shelly.devices.ShellyBLU import ShellyBLU
from shelly.devices.ShellyBLUDoorWindow import ShellyBLUDoorWindow
from shelly.devices.ShellyBLUButton1 import ShellyBLUButton1
from shelly.devices.ShellyBLUDistance import ShellyBLUDistance
from shelly.devices.ShellyPlus1 import ShellyPlus1
from shelly.devices.ShellyPlus1PM import ShellyPlus1PM
from shelly.devices.ShellyPlus2PM import ShellyPlus2PM
from shelly.devices.ShellyPlusWallDimmer import ShellyPlusWallDimmer
from shelly.devices.ShellyPlusI4 import ShellyPlusI4
from shelly.devices.ShellyPlusHT import ShellyPlusHT
from shelly.devices.ShellyPlusPlug import ShellyPlusPlug
from shelly.devices.ShellyPlusPlugS import ShellyPlusPlugS
from shelly.devices.ShellyPlusPlugUK import ShellyPlusPlugUK
from shelly.devices.ShellyPro1 import ShellyPro1
from shelly.devices.ShellyPro1PM import ShellyPro1PM
from shelly.devices.ShellyPro2 import ShellyPro2
from shelly.devices.ShellyPro2PM import ShellyPro2PM
from shelly.devices.ShellyPro4PM import ShellyPro4PM
from shelly.devices.ShellyPlus2PMGen3 import ShellyPlus2PMGen3

shelly_model_classes = {
    'shelly-blu-doorwindow': ShellyBLUDoorWindow,
    'shelly-blu-button1': ShellyBLUButton1,
    'shelly-blu-distance': ShellyBLUDistance,
    'shelly-plus-1': ShellyPlus1,
    'shelly-plus-1-pm': ShellyPlus1PM,
    'shelly-plus-2-pm': ShellyPlus2PM,
    'shelly-plus-walldimmer': ShellyPlusWallDimmer,
    'shelly-plus-i-4': ShellyPlusI4,
    'shelly-plus-ht': ShellyPlusHT,
    'shelly-plus-plug': ShellyPlusPlug,
    'shelly-plus-plug-s': ShellyPlusPlugS,
    'shelly-plus-plug-uk': ShellyPlusPlugUK,
    'shelly-pro-1': ShellyPro1,
    'shelly-pro-1-pm': ShellyPro1PM,
    'shelly-pro-2': ShellyPro2,
    'shelly-pro-2-pm': ShellyPro2PM,
    'shelly-pro-4-pm': ShellyPro4PM,
    'shelly-plus-2-pm-gen3': ShellyPlus2PMGen3,
}


class Plugin(indigo.PluginBase):
    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        super(Plugin, self).__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self.setLogLevel(pluginPrefs.get('log-level', "info"))

        self.shellies = {}
        self.triggers = {}
        self.message_types = []
        self.message_queue = Queue()
        self.mqtt_plugin = indigo.server.getPlugin("com.flyingdiver.indigoplugin.mqtt")

        # {
        #   <brokerId>: {
        #       'some/topic': [dev1, dev2, dev3],
        #       'another/topic': [dev1, dev4, dev5]
        #   }
        #   <anotherBroker>: {
        #       'some/topic': [dev6, dev7],
        #       'another/unique/topic': [dev7, dev8, dev9]
        #   }
        # }
        self.broker_device_topics = {}

        self.discovered_blu_addresses = set()
        """The set of discovered BLU addresses"""

        self.blu_address_device: dict[str, int] = {}
        """Mapping from a BLU address to the indigo device id"""

    def startup(self):
        """

        :return:
        """

        if not self.mqtt_plugin:
            self.logger.error("MQTT Connector plugin is required!!")
            exit(-1)
        indigo.server.subscribeToBroadcast("com.flyingdiver.indigoplugin.mqtt",
                                           "com.flyingdiver.indigoplugin.mqtt-message_queued", "message_handler")
        self.logger.info(self.pluginFolderPath)

    def shutdown(self):
        pass

    def runConcurrentThread(self):
        """
        Main work thread where messages are continually dequeued and processed.

        :return: None
        """

        try:
            while True:
                if not self.mqtt_plugin.isEnabled():
                    self.logger.error("MQTT Connector plugin not enabled, aborting.")
                    self.sleep(60)
                else:
                    self.process_messages()
                    self.sleep(0.1)

        except self.StopThread:
            pass

    #
    # Message processing
    #

    def message_handler(self, notification):
        """
        Handler to receive and queue messages coming from the mqtt plugin.

        :param notification: The message object.
        :return: None
        """

        if notification['message_type'] in self.message_types:
            self.message_queue.put(notification)

    def process_messages(self):
        """
        Processes messages in the queue until the queue is empty. This is used to pass
        messages that have come from MQTT into the appropriate devices.

        :return: None
        """

        while not self.message_queue.empty():
            # At least 1 of the devices care about this message
            notification = self.message_queue.get()
            if not notification:
                return

            # We have a valid message
            # Find the devices that need to get this message and give it to them
            broker_id = int(notification['brokerID'])
            props = {'message_type': notification['message_type']}
            while True:
                data = self.mqtt_plugin.executeAction("fetchQueuedMessage", deviceId=broker_id, props=props, waitUntilDone=True)
                if data is None:  # Ensure we got data back
                    break

                topic = '/'.join(data['topic_parts'])  # transform the topic into a single string
                payload = data['payload']
                message_type = data['message_type']
                self.logger.debug("    Processing: \"%s\" on topic \"%s\"", payload, topic)
                device_topics = self.broker_device_topics.get(broker_id, {})  # get device topics for this broker
                dev_ids = device_topics.get(topic, list())  # get devices listening on this broker for this topic
                for dev_id in dev_ids:
                    shelly = self.shellies.get(dev_id, None)
                    self.logger.debug(shelly)
                    if shelly is not None and message_type == shelly.get_message_type():
                        # Send this message data to the shelly object
                        shelly.handle_message(topic, payload)

    #
    # Device management
    #

    def deviceStartComm(self, device):
        """

        :param device:
        :return:
        """
        # Catch main devices that have already been started by a component
        if device.id in self.shellies:
            self.logger.debug("{} not starting again...".format(device.name))
            return

        if device.deviceTypeId in shelly_model_classes:
            model_class = shelly_model_classes[device.deviceTypeId]
            shelly = model_class(device.id)
            self.shellies[device.id] = shelly

            if device.deviceTypeId.startswith("shelly-blu"):
                # Ensure the BLU device has a MAC address defined
                if shelly.get_address() is None:
                    self.logger.error(f"'{device.name}' is not properly setup! Address is unknown.")
                    return
                
                # Track the device associated with the BLU address
                self.blu_address_device[shelly.get_address()] = device.id
            else:
                # Check that the device has a broker and an address
                if shelly.get_broker_id() is None or shelly.get_address() is None:
                    # Ensure the device has a broker and address
                    self.logger.error("broker-id: \"{}\" address: \"{}\"".format(shelly.get_broker_id(), shelly.get_address()))
                    self.logger.error("\"{}\" is not properly setup! Check the broker and topic root.".format(device.name))
                    return False

                # ensure that deviceSubscriptions has a dictionary of subscriptions for the broker
                if shelly.get_broker_id() not in self.broker_device_topics:
                    self.broker_device_topics[shelly.get_broker_id()] = {}

                # Add the device id to each topic list
                broker_topics = self.broker_device_topics[shelly.get_broker_id()]
                for topic in shelly.get_topics():
                    # See if there is a list of devices already listening to this subscription
                    if topic not in broker_topics:
                        # initialize a new list of devices for this subscription
                        broker_topics[topic] = []
                    broker_topics[topic].append(shelly.device.id)

                # Store the message type
                self.message_types.append(shelly.get_message_type())
        else:
            # This is a component device starting
            # if none of the devices in the device group are in self.shellies yet,
            # then the main device has not tarted
            grouped_with = indigo.device.getGroupList(device)
            main_device_has_started = False
            for grouped_with_device_id in grouped_with:
                if grouped_with_device_id in self.shellies:
                    main_device_has_started = True
                    break

            # go through all the grouped devices and see if any are main devices
            # and start any that are before starting the component
            if not main_device_has_started:
                for grouped_with_device_id in grouped_with:
                    grouped_with_device = indigo.devices[grouped_with_device_id]
                    if grouped_with_device.deviceTypeId in shelly_model_classes:
                        self.logger.debug("{} needs it's main device started, starting '{}' manually...".format(device.name, grouped_with_device.name))
                        self.deviceStartComm(grouped_with_device)

        # Refresh the device's state list and properties that may have changed between plugin versions
        device = indigo.device.changeDeviceTypeId(device, device.deviceTypeId)
        device.replaceOnServer()
        props = device.pluginProps
        device.replacePluginPropsOnServer(props)
        device.stateListOrDisplayStateIdChanged()

        # Update the config at the end if it is the main device
        if device.id in self.shellies:
            shelly = self.shellies[device.id]
            if isinstance(shelly, ShellyBLU):
                return
            shelly.get_config()

            # Get the status of all components across sub-devices
            for component in shelly.components:
                component.get_status()

    def deviceStopComm(self, device):
        """

        :param device:
        :return:
        """

        if device.id in self.shellies:
            shelly = self.shellies[device.id]

            if isinstance(shelly, Shelly):
                # make sure that the device's broker has subscriptions
                if shelly.get_broker_id() in self.broker_device_topics:
                    broker_topics = self.broker_device_topics[shelly.get_broker_id()]
                    for topic in shelly.get_topics():
                        if topic in broker_topics and shelly.device.id in broker_topics[topic]:
                            broker_topics[topic].remove(shelly.device.id)

                    # remove the broker key if there are no more devices using it
                    if len(broker_topics) == 0:
                        del self.broker_device_topics[shelly.get_broker_id()]

                self.message_types.remove(shelly.get_message_type())

            try:
                del self.shellies[device.id]
            except KeyError:
                self.logger.warning("Something went wrong! '{}' could not be removed from internal shellies dictionary".format(device.name))

    def didDeviceCommPropertyChange(self, orig_dev, new_dev):
        """
        This method gets called by the default implementation of deviceUpdated() to determine if
        any of the properties needed for device communication (or any other change requires a
        device to be stopped and restarted). The default implementation checks for any changes to
        properties. You can implement your own to provide more granular results. For instance, if
        your device requires 4 parameters, but only 2 of those parameters requires that you restart
        the device, then you can check to see if either of those changed. If they didn't then you
        can just return False and your device won't be restarted (via deviceStopComm()/deviceStartComm() calls).

        :param orig_dev: The device before updates.
        :param new_dev: The device after updates.
        :return: True or false whether the device had the communication properties changed.
        """

        if new_dev.deviceTypeId in shelly_model_classes:
            # The final device is a main device, so it needs to be restarted
            # if any of the MQTT information changes
            restart_fields = ["broker-id", "address", "message-type", "blu-relay"]
            for field in restart_fields:
                if new_dev.pluginProps.get(field, None) != orig_dev.pluginProps.get(field, None):
                    return True
        return False

    def triggerStartProcessing(self, trigger):
        """
        Called when a new trigger should be processed by the plugin.
        :param trigger: The trigger reference
        :return:
        """

        self.triggers[trigger.id] = trigger

    def triggerStopProcessing(self, trigger):
        """
        Called when a new trigger should stop being processed by the plugin.
        :param trigger: The trigger reference
        :return:
        """

        del self.triggers[trigger.id]

    def getDeviceFactoryUiValues(self, dev_id_list):
        """
        Get the values and errors that should be displayed in the Device
        Factory UI for a given device group.

        If a main device can be found in the group, then the Shelly device
        model, broker-id, address, and message-type fields are populated from
        the main device.

        :param dev_id_list: The list of device id's in the device group.
        :return: Tuple of a dict of values to set in the UI and a dict of
        errors for each field to display.
        """

        values_dict = indigo.Dict()
        errors_dict = indigo.Dict()

        main_device = None
        # Find the main device in the group
        for dev_id in dev_id_list:
            device = indigo.devices[dev_id]
            if device.deviceTypeId in shelly_model_classes:
                main_device = device
                break

        if main_device is None:
            return values_dict, errors_dict
        
        values_dict['shelly-model'] = main_device.deviceTypeId
        values_dict['is-initial-setup'] = main_device.pluginProps.get('is-initial-setup', False)

        if main_device.deviceTypeId.startswith("shelly-blu"):
            values_dict['is-mqtt-model'] = False
            values_dict['is-blu-model'] = True
            values_dict['mac-address'] = main_device.pluginProps.get('address')
        else:
            values_dict['is-mqtt-model'] = True
            values_dict['is-blu-model'] = False
            values_dict['broker-id'] = main_device.pluginProps.get('broker-id')
            values_dict['address'] = main_device.pluginProps.get('address')
            values_dict['message-type'] = main_device.pluginProps.get('message-type')

        return values_dict, errors_dict

    def validateDeviceFactoryUi(self, values_dict, dev_id_list):
        """

        :param values_dict: The data the user is attempting to submit from the
        device factory UI.
        :param dev_id_list: The list of device id's in the device group.
        :return: Tuple of validity status, data to load into the form, and any
        error messages.
        """

        errors_dict = indigo.Dict()
        valid = True
        group_device_types = [indigo.devices[dev_id].deviceTypeId for dev_id in dev_id_list]

        # Ensure a model is selected
        if values_dict['shelly-model'] == "":
            valid = False
            errors_dict['shelly-model'] = "You must select the device model!"
        else:
            # Ensure the main model is not changing
            main_models = set(group_device_types).intersection(set(shelly_model_classes.keys()))
            if len(main_models) == 1:
                # The selected model must be the same
                main_model = main_models.pop()
                if values_dict['shelly-model'] != main_model:
                    valid = False
                    errors_dict['shelly-model'] = "You can not change the main model!"

        if values_dict.get("is-mqtt-model", True):
            # Ensure a broker is selected
            if values_dict['broker-id'] == "":
                valid = False
                errors_dict['broker-id'] = "You must select the broker the device will connect to!"

            # Ensure the device address is supplied
            if values_dict['address'] == "":
                valid = False
                errors_dict['address'] = "You must specify the base device address!"

            # Ensure the message type is supplied
            if values_dict['message-type'] == "":
                valid = False
                errors_dict['message-type'] = "You must specify the message type for the device!"
        elif values_dict.get("is-blu-model", False):
            address = values_dict.get("mac-address", None)
            if not address:
                errors_dict["mac-address"] = "You must specify a MAC address!"
                valid = False
            elif not re.match(r"^[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}$", address.lower()):
                errors_dict["mac-address"] = "MAC address does not match format! ex: 38:39:8f:a0:1d:a6"
                valid = False

        return valid, values_dict, errors_dict

    def closedDeviceFactoryUi(self, values_dict, user_cancelled, dev_id_list):
        """Handler to process data when the Device Factory UI is closed.

        If the user cancelled the UI (not saving) then no processing is done on
        the data.

        A list of device models (components) is extracted from each device in
        the device group. This helps determine if the device group has all
        required sub-devices in the group.The shelly model is pulled from the
        fields and the corresponding device class is loaded.

        If the main device is not found in the group then it will be created.
        Upon creation, the required sub-devices will also be created and
        automatically assigned to the same device group (because the Device
        Factory UI is opened).

        If the main device model is already in the device group then it will be
        found and stored for reference later.

        The main device is either created or found, so the properties entered
        into the UI are applied to this device. The main device stores any MQTT
        information and acts as a hub for any component in a sub-device to
        communicate through.

        :param values_dict: A dictionary of values for each field in the UI.
        :param user_cancelled: A boolean to indicate if the user saved the UI
        or closed it without saving.
        :param dev_id_list: A list of device id's in the device group.
        :return:
        """

        if user_cancelled:
            return

        shelly_model = values_dict['shelly-model']
        group_models = [indigo.devices[dev_id].model for dev_id in dev_id_list]
        model_class = shelly_model_classes.get(shelly_model, None)
        if model_class is None:
            self.logger.error("Unable to find class for device with type: '{}'".format(shelly_model))

        if shelly_model.startswith("shelly-blu"):
            main_device = None
            device_props = {
                'address': values_dict["mac-address"],
                'is-initial-setup': False
            }
            if model_class.display_name not in group_models:
                # The main device is not in the group, so create one
                main_device = indigo.device.create(
                    indigo.kProtocol.Plugin,
                    name=values_dict["initial-name"] or None,
                    deviceTypeId=shelly_model,
                    props=device_props
                )
                main_device.model = model_class.display_name
                main_device.replaceOnServer()
            else:
                # Find the main device in the group
                for dev_id in dev_id_list:
                    main_device = indigo.devices[dev_id]
                    if main_device.deviceTypeId == shelly_model:
                        break
            
            # Make sure we actually have a main device
            if main_device is None:
                self.logger.error("Unable to create or find the main device!")
                return

            # Update the device properties from the factory UI
            main_device.replacePluginPropsOnServer(device_props)

            # Initialize the device and its components to populate the already opened UI
            model_class(main_device.id)
        else:
            main_device = None
            device_props = {
                'broker-id': values_dict["broker-id"],
                'address': values_dict["address"],
                'message-type': values_dict["message-type"],
                'is-initial-setup': False,
                'profile': values_dict.get("profile")
            }
            if model_class.display_name not in group_models:
                # The main device is not in the group, so create one
                main_device = indigo.device.create(
                    indigo.kProtocol.Plugin,
                    name=values_dict["initial-name"] or None,
                    deviceTypeId=shelly_model,
                    props=device_props
                )
                main_device.model = model_class.display_name
                main_device.replaceOnServer()
            else:
                # Find the main device in the group
                for dev_id in dev_id_list:
                    main_device = indigo.devices[dev_id]
                    if main_device.deviceTypeId == shelly_model:
                        break

            # Make sure we actually have a main device
            if main_device is None:
                self.logger.error("Unable to create or find the main device!")
                return

            # Update the device properties from the factory UI
            main_device.replacePluginPropsOnServer(device_props)

            # Initialize the device and its components to populate the already opened UI
            model_class(main_device.id)
    
    def deviceFactoryModelChanged(self, valuesDict, typeId, devId):
        model:str = valuesDict.get("shelly-model", "")
        if model.startswith("shelly-blu"):
            valuesDict["is-mqtt-model"] = False
            valuesDict["is-blu-model"] = True
        else:
            valuesDict["is-mqtt-model"] = True
            valuesDict["is-blu-model"] = False
        
        return valuesDict

    def validateDeviceConfigUi(self, values_dict, type_id, dev_id):
        """

        :param values_dict:
        :param type_id:
        :param dev_id:
        :return:
        """

        return True, values_dict

    def closedDeviceConfigUi(self, values_dict, user_cancelled, type_id, dev_id):
        """

        :param values_dict:
        :param user_cancelled:
        :param type_id:
        :param dev_id:
        :return:
        """

        # When a config is changed then the device already tries to get the config
        # TODO: Determine if the device needs to be restarted from the config change
        """
        if dev_id in self.shellies:
            self.shellies[dev_id].get_config()
        else:
            component = self.get_component(indigo.devices[dev_id])
            if component:
                self.logger.info("Getting config of component: {} ({})".format(component, component.device.name))
                component.get_config()
            else:
                self.logger.error("Unable to find shelly or component for device id: {}".format(dev_id))
        """
        pass

    def actionControlDevice(self, action, device):
        """
        Handles an action being performed on the device.

        :param action: The action that occurred.
        :param device: The device that was acted on.
        :return: None
        """
        shelly = self.shellies.get(device.id, None)
        if shelly is not None:
            shelly.handle_action(action)
        else:
            component = self.get_component(device)
            if component is not None:
                component.handle_action(action)

    def actionControlUniversal(self, action, device):
        """Handles an action being performed on the device.

        The main Shelly device or component is identified based on the device
        and passed the action to handle at the component-level.

        :param action: The action that occurred.
        :param device: The device that was acted on.
        :return: None
        """
        shelly = self.shellies.get(device.id, None)
        if shelly is not None:
            shelly.handle_action(action)
        else:
            component = self.get_component(device)
            if component is not None:
                component.handle_action(action)

    def getDeviceStateList(self, device):
        """
        Generate a list of states for the Indigo device.

        The method ``get_device_state_list()`` is called on the Shelly or
        Component associated with the device. If the device was not found to be
        associated with an object, then a default state list is returned.

        :param device: The Indigo device.
        :return: A list of states for the device.
        """
        shelly = self.shellies.get(device.id, None)
        if shelly:
            return shelly.get_device_state_list()
        else:
            component = self.get_component(device)
            if component:
                return component.get_device_state_list()
            else:
                return indigo.PluginBase.getDeviceStateList(self, device)

    def getDeviceDisplayStateId(self, device):
        """
        Generate the state to show for the Indigo device in the devices list.

        The method ``get_device_display_state_id()`` is called on the Shelly or
        Component associated with the device. If the device was not found to be
        associated with an object, then a default state list is returned.

        The default state will be used if the device does not implement the method.

        :param device: The Indigo device.
        :return: A list of states for the device.
        """
        shelly = self.shellies.get(device.id, None)
        if shelly:
            return shelly.get_device_display_state_id()
        else:
            component = self.get_component(device)
            if component:
                return component.get_device_display_state_id()
            else:
                return indigo.PluginBase.getDeviceDisplayStateId(self, device)

    #
    # Plugin utilities
    #

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        """
        Handler for the closing of a configuration UI.

        :param valuesDict: The values in the config.
        :param userCancelled: True or false to indicate if the config was cancelled.
        :return:
        """

        if userCancelled is False:
            self.setLogLevel(valuesDict.get('log-level', "info"))

    def get_shelly_devices(self, filter="", valuesDict=None, typeId="", targetId=0):
        """

        :return:
        """
        _shellies = []
        if filter:
            types = [cat.strip() for cat in filter.split(",")]
            for dev_type in types:
                for dev in indigo.devices.iter(dev_type):
                    _shellies.append((dev.id, dev.name))
        else:
            for dev in indigo.devices.iter("self"):
                device = self.shellies.get(dev.id, None)
                if device and isinstance(device, Shelly):
                    _shellies.append((dev.id, dev.name))

        _shellies.sort(key=lambda d: d[1])
        return _shellies

    def get_broker_devices(self, filter="", valuesDict=None, typeId="", targetId=0):
        """
        Gets a list of available broker devices.

        :return: A list of brokers.
        """

        brokers = []
        for dev in indigo.devices.iter():
            if dev.protocol == indigo.kProtocol.Plugin and \
                    dev.pluginId == "com.flyingdiver.indigoplugin.mqtt" and \
                    dev.deviceTypeId != 'aggregator':
                brokers.append((dev.id, dev.name))

        brokers.sort(key=lambda tup: tup[1])
        return brokers
    
    def get_discovered_blu_addresses(self, filter="", valuesDict=None, typeId="", targetId=0):
        addresses = []
        if len(self.discovered_blu_addresses) == 0:
            addresses.append(("none", "%%disabled:No discovered BLU devices%%"))
        else:
            for address in self.discovered_blu_addresses:
                addresses.append((address, address))
        return addresses
    
    def refresh(self, valuesDict, typeId, devId):
        return valuesDict
    
    def select_discovered_blu_address(self, valuesDict, typeId, devId):
        valuesDict["mac-address"] = valuesDict.get("discovered-mac-address", "")
        return valuesDict

    def get_component(self, device):
        """
        Gets the component tied to a device.

        :param device: The indigo device.
        :return: Component object for the device.
        """

        for shelly in self.shellies.values():
            if not isinstance(shelly, Shelly):
                continue
            component = shelly.get_component_for_device(device)
            if component is not None:
                return component
        return None

    def setLogLevel(self, level):
        """
        Helper method to set the logging level.

        :param level: Expected to be a string with a valid log level.
        :return: None
        """

        valid_log_levels = ["debug", "info", "warning"]
        if level not in valid_log_levels:
            self.logger.error(u"Attempted to set the log level to an unhandled value: {}".format(level))

        if level == "debug":
            self.indigo_log_handler.setLevel(logging.DEBUG)
            self.logger.debug(u"Log level set to debug")
        elif level == "info":
            self.indigo_log_handler.setLevel(logging.INFO)
            self.logger.info(u"Log level set to info")
        elif level == "warning":
            self.indigo_log_handler.setLevel(logging.WARNING)
            self.logger.warning(u"Log level set to warning")

    #
    # Device Configuration Callbacks
    #

    def _write_system_configuration(self, values_dict, type_id, dev_id):
        """
        Handler for writing the system component's configuration.

        :param values_dict:
        :param type_id:
        :param dev_id:
        :return:
        """

        shelly = self.shellies.get(dev_id, None)
        if not shelly:
            return

        config = {
            'device': {
                'name': values_dict.get("system-device-name"),
                'eco_mode': values_dict.get("system-device-eco-mode")
            },
            'location': {
                'tz': values_dict.get("system-location-timezone"),
                'lat': values_dict.get("system-location-lat"),
                'lon': values_dict.get("system-location-lon")
            },
            'debug': {
                'mqtt': {
                    'enable': values_dict.get("system-debug-mqtt")
                },
                'websocket': {
                    'enable': values_dict.get("system-debug-mqtt")
                },
                'udp': {
                    'addr': values_dict.get("system-debug-udp-address")
                }
            }
        }

        if len(config['device']['name']) == 0:
            config['device']['name'] = None
        if len(config['location']['tz']) == 0:
            config['location']['tz'] = None
        if config['location']['lat'] == "":
            config['location']['lat'] = None
        if config['location']['lon'] == "":
            config['location']['lon'] = None
        if len(config['debug']['udp']['addr']) == 0:
            config['debug']['udp']['addr'] = None

        shelly.system_components['system'].set_config(config)

    def _write_wifi_configuration(self, values_dict, type_id, dev_id):
        """
        Handler for writing the wifi component's configuration.

        :param values_dict:
        :param type_id:
        :param dev_id:
        :return:
        """

        shelly = self.shellies.get(dev_id, None)
        if not shelly:
            return

        config = {
            'ap': {
                'ssid': values_dict.get("wifi-ap-ssid"),
                'pass': values_dict.get("wifi-ap-password"),
                'is_open': values_dict.get("wifi-ap-open-network"),
                'enable': values_dict.get("wifi-ap-enable")
            },
            'sta': {
                'ssid': values_dict.get("wifi-1-ssid"),
                'pass': values_dict.get("wifi-1-password"),
                'is_open': values_dict.get("wifi-1-open-network"),
                'enable': values_dict.get("wifi-1-enable"),
                'ipv4mode': values_dict.get("wifi-1-ipv4-mode"),
                'ip': values_dict.get("wifi-1-ip-address"),
                'netmask': values_dict.get("wifi-1-network-mask"),
                'gw': values_dict.get("wifi-1-gateway"),
                'nameserver': values_dict.get("wifi-1-nameserver")
            },
            'sta1': {
                'ssid': values_dict.get("wifi-2-ssid"),
                'pass': values_dict.get("wifi-2-password"),
                'is_open': values_dict.get("wifi-2-open-network"),
                'enable': values_dict.get("wifi-2-enable"),
                'ipv4mode': values_dict.get("wifi-2-ipv4-mode"),
                'ip': values_dict.get("wifi-2-ip-address"),
                'netmask': values_dict.get("wifi-2-network-mask"),
                'gw': values_dict.get("wifi-2-gateway"),
                'nameserver': values_dict.get("wifi-2-nameserver")
            },
            'roam': {
                'rssi_thr': values_dict.get("wifi-roaming-rssi-threshold"),
                'interval': values_dict.get("wifi-roaming-interval")
            }
        }

        # Don't write passwords if they are blank since a get_config will not populate them
        if len(config['ap']['pass']) == 0:
            del config['ap']['pass']
        if len(config['sta']['pass']) == 0:
            del config['sta']['pass']
        if len(config['sta1']['pass']) == 0:
            del config['sta1']['pass']

        # ip, netmask, gw, and nameserver should be None when empty
        # wifi 1
        if len(config['sta']['ssid']) == 0:
            config['sta']['ssid'] = None
        if len(config['sta']['ip']) == 0:
            config['sta']['ip'] = None
        if len(config['sta']['netmask']) == 0:
            config['sta']['netmask'] = None
        if len(config['sta']['gw']) == 0:
            config['sta']['gw'] = None
        if len(config['sta']['nameserver']) == 0:
            config['sta']['nameserver'] = None
        # wifi 2
        if len(config['sta1']['ssid']) == 0:
            config['sta1']['ssid'] = None
        if len(config['sta1']['ip']) == 0:
            config['sta1']['ip'] = None
        if len(config['sta1']['netmask']) == 0:
            config['sta1']['netmask'] = None
        if len(config['sta1']['gw']) == 0:
            config['sta1']['gw'] = None
        if len(config['sta1']['nameserver']) == 0:
            config['sta1']['nameserver'] = None

        shelly.system_components['wifi'].set_config(config)

    def _write_ble_configuration(self, values_dict, type_id, dev_id):
        """
        Handler for writing the BLE component's configuration.

        :param values_dict:
        :param type_id:
        :param dev_id:
        :return:
        """

        shelly = self.shellies.get(dev_id, None)
        if not shelly:
            return

        config = {
            'enable': values_dict.get("ble-enable")
        }

        shelly.system_components['ble'].set_config(config)

    def _write_htui_configuration(self, values_dict, type_id, dev_id):
        """
        Handler for writing the HT_UI component's configuration.

        :param values_dict:
        :param type_id:
        :param dev_id:
        :return:
        """

        shelly = self.shellies.get(dev_id, None)
        if not shelly:
            return

        config = {
            'temperature_unit': values_dict.get("ht-ui-temperature-unit")
        }

        shelly.system_components['ht-ui'].set_config(config)

    def _write_switch_configuration(self, values_dict, type_id, dev_id):
        """
        Handler for writing the switch component's configuration.

        :param values_dict:
        :param type_id:
        :param dev_id:
        :return:
        """

        switch = self.get_component(indigo.devices[dev_id])

        config = {
            'name': values_dict.get("name", ""),
            'in_mode': values_dict.get("in-mode", ""),
            'initial_state': values_dict.get("initial-state", ""),
            'auto_on': values_dict.get("auto-on", False),
            'auto_on_delay': values_dict.get("auto-on-delay", ""),
            'auto_off': values_dict.get("auto-off", False),
            'auto_off_delay': values_dict.get("auto-off-delay", ""),
            'input_id': values_dict.get("input-id", ""),
            'power_limit': values_dict.get("power-limit", ""),
            'voltage_limit': values_dict.get("voltage-limit", ""),
            'current_limit': values_dict.get("current-limit", ""),
        }

        if len(config['name']) == 0:
            config['name'] = None

        switch.set_config(config)

    def _write_cover_configuration(self, values_dict, type_id, dev_id):
        """
        Handler for writing the cover component's configuration.

        :param values_dict:
        :param type_id:
        :param dev_id:
        :return: None
        """

        cover = self.get_component(indigo.devices[dev_id])

        config = {
            'name': values_dict.get("name"),
            'in_mode': values_dict.get("in-mode", ""),
            'initial_state': values_dict.get("initial-state", ""),
        }

        cover.set_config(config)

    def _write_input_configuration(self, values_dict, type_id, dev_id):
        """
        Handler for writing the input component's configuration.

        :param values_dict:
        :param type_id:
        :param dev_id:
        :return:
        """

        input_component = self.get_component(indigo.devices[dev_id])

        config = {
            'name': values_dict.get("name", ""),
            'type': values_dict.get("type", ""),
            'invert': values_dict.get("invert", False)
        }

        if len(config['name']) == 0:
            config['name'] = None

        input_component.set_config(config)

    def action_handler(self, pluginAction=None, device=None, callerWaitingForResult=False):
        """

        :param pluginAction:
        :param device:
        :param callerWaitingForResult:
        :return:
        """
        action = pluginAction.pluginTypeId

        # Helper code to extract a shelly device from a menu
        device_id = pluginAction.props.get("device-id", None)
        if device_id:
            try:
                device_id = int(device_id)
            except TypeError:
                device_id = None
        shelly = self.shellies.get(device_id, None)

        # Handle individual actions
        if action == "Shelly.CheckForUpdate":
            if shelly:
                shelly.check_for_update()
        elif action == "Shelly.Update":
            if shelly:
                stage = pluginAction.props.get("stage")

                shelly.update(stage)
