#author Phil
#created 2015-10-22

from adl3 import *
import collections, os

#based on show_status from atitweak. returns an object with all info
def get_adapters(adapter_list=None):
    adapters = list()
    adapter_info = get_adapter_info()

    for index, info in enumerate(adapter_info):
        if adapter_list is None or index in adapter_list:
            adapter = dict()
            adapter['index'] = index
            adapter['adapterName'] = info.strAdapterName
            adapter['displayName'] = info.strDisplayName

            activity = ADLPMActivity()
            activity.iSize = sizeof(activity)

            if ADL_Overdrive5_CurrentActivity_Get(info.iAdapterIndex, byref(activity)) != ADL_OK:
                raise ADLError("ADL_Overdrive5_CurrentActivity_Get failed.")

            adapter['engineClock'] = activity.iEngineClock/100.0
            adapter['memoryClock'] = activity.iMemoryClock/100.0
            adapter['coreVoltage'] = activity.iVddc/1000.0
            adapter['performanceLevel'] = activity.iCurrentPerformanceLevel
            adapter['utilization'] = activity.iActivityPercent

            fan_speed = {}
            for speed_type in (ADL_DL_FANCTRL_SPEED_TYPE_PERCENT, ADL_DL_FANCTRL_SPEED_TYPE_RPM):
                fan_speed_value = ADLFanSpeedValue()
                fan_speed_value.iSize = sizeof(fan_speed_value)
                fan_speed_value.iSpeedType = speed_type

                if ADL_Overdrive5_FanSpeed_Get(info.iAdapterIndex, 0, byref(fan_speed_value)) != ADL_OK:
                    fan_speed[speed_type] = None
                    continue

                fan_speed[speed_type] = fan_speed_value.iFanSpeed
                user_defined = fan_speed_value.iFlags & ADL_DL_FANCTRL_FLAG_USER_DEFINED_SPEED

            adapter['fanPercent'] = -1
            if bool(fan_speed[ADL_DL_FANCTRL_SPEED_TYPE_PERCENT]):
                adapter['fanPercent'] = fan_speed[ADL_DL_FANCTRL_SPEED_TYPE_PERCENT]
            adapter['fanRPM'] = -1
            if bool(fan_speed[ADL_DL_FANCTRL_SPEED_TYPE_RPM]):
                adapter['fanRPM'] = fan_speed[ADL_DL_FANCTRL_SPEED_TYPE_RPM]
            adapter['fanIsUserDefined'] = True if user_defined else False

            temperature = ADLTemperature()
            temperature.iSize = sizeof(temperature)

            if ADL_Overdrive5_Temperature_Get(info.iAdapterIndex, 0, byref(temperature)) != ADL_OK:
                raise ADLError("ADL_Overdrive5_Temperature_Get failed.")
            adapter['temperature'] = temperature.iTemperature/1000.0

            # Powertune level
            powertune_level_value = c_int()
            dummy = c_int()

            if ADL_Overdrive5_PowerControl_Get(info.iAdapterIndex, byref(powertune_level_value), byref(dummy)) != ADL_OK:
                raise ADLError("ADL_Overdrive5_PowerControl_Get failed.")
            adapter['powertune'] = powertune_level_value.value

            adapters.append(adapter)
    return adapters

#copied from atitweak
def get_adapter_info():
    adapter_info = []
    num_adapters = c_int(-1)
    if ADL_Adapter_NumberOfAdapters_Get(byref(num_adapters)) != ADL_OK:
        raise ADLError("ADL_Adapter_NumberOfAdapters_Get failed.")

    # allocate an array of AdapterInfo, see ctypes docs for more info
    AdapterInfoArray = (AdapterInfo * num_adapters.value)()

    # AdapterInfo_Get grabs info for ALL adapters in the system
    if ADL_Adapter_AdapterInfo_Get(cast(AdapterInfoArray, LPAdapterInfo), sizeof(AdapterInfoArray)) != ADL_OK:
        raise ADLError("ADL_Adapter_AdapterInfo_Get failed.")

    deviceAdapter = collections.namedtuple('DeviceAdapter', ['AdapterIndex', 'AdapterID', 'BusNumber', 'UDID'])
    devices = []

    for adapter in AdapterInfoArray:
        index = adapter.iAdapterIndex
        busNum = adapter.iBusNumber
        udid = adapter.strUDID

        adapterID = c_int(-1)
        #status = c_int(-1)

        if ADL_Adapter_ID_Get(index, byref(adapterID)) != ADL_OK:
            raise ADLError("ADL_Adapter_Active_Get failed.")

        found = False
        for device in devices:
            if (device.AdapterID.value == adapterID.value):
                found = True
                break

        # save it in our list if it's the first controller of the adapter
        if (found == False):
            devices.append(deviceAdapter(index,adapterID,busNum,udid))

    for device in devices:
        adapter_info.append(AdapterInfoArray[device.AdapterIndex])

    return adapter_info

#copied from atitweak
def initialize():
    # check for unset DISPLAY, assume :0
    if "DISPLAY" not in os.environ:
        os.environ["DISPLAY"] = ":0"

    # the '1' means only retrieve info for active adapters
    if ADL_Main_Control_Create(ADL_Main_Memory_Alloc, 1) != ADL_OK:
        raise ADLError("Couldn't initialize ADL interface.")

#copied from atitweak
def shutdown():
    if ADL_Main_Control_Destroy() != ADL_OK:
        raise ADLError("Couldn't destroy ADL interface global pointers.")
