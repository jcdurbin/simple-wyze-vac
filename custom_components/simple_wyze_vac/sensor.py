from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.const import ATTR_BATTERY_LEVEL, PERCENTAGE


from .const import DOMAIN, WYZE_VACUUMS

async def async_setup_entry(
    hass,
    config_entry,
    async_add_entities,
) -> None:
    """Set up the demo sensor platform."""
    async_add_entities(
        [BatterySensor(pl["name"]+"_battery",
                       wyze_battery=pl["battery"],
                       state=None,
                       device_name=pl["name"]+"_battery",
                       device_class=SensorDeviceClass.BATTERY,
                       unit_of_measurement=PERCENTAGE) for pl in hass.data[WYZE_VACUUMS]]
    )

class BatterySensor(SensorEntity):
    """Representation of a Demo sensor."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False

    def __init__(
        self,
        unique_id: str,
        wyze_battery,
        device_name: str | None,
        state: float | str | None,
        device_class: SensorDeviceClass,
        unit_of_measurement: str | None,
        options: list[str] | None = None,
        translation_key: str | None = None,
    ) -> None:
        """Initialize the sensor."""
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit_of_measurement
        self._attr_native_value = state
        self._attr_unique_id = unique_id
        self._attr_options = options
        self._attr_translation_key = translation_key

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, unique_id)},
            name=device_name,
        )

        self._wyze_battery = wyze_battery

        self._attr_extra_state_attributes = {ATTR_BATTERY_LEVEL: wyze_battery.value}

    @property
    def native_value(self):
        return self._wyze_battery.value