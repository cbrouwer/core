"""Calculates mold growth indication from temperature and humidity."""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant import util
from homeassistant.components.sensor import (
    PLATFORM_SCHEMA as SENSOR_PLATFORM_SCHEMA,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_NAME,
    EVENT_HOMEASSISTANT_START,
    PERCENTAGE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType, StateType
from homeassistant.util.unit_conversion import TemperatureConverter
from homeassistant.util.unit_system import METRIC_SYSTEM

from .const import (
    CONF_CALIBRATION_FACTOR,
    CONF_INDOOR_HUMIDITY,
    CONF_INDOOR_TEMP,
    CONF_OUTDOOR_TEMP,
    DEFAULT_NAME,
)

_LOGGER = logging.getLogger(__name__)

ATTR_CRITICAL_TEMP = "estimated_critical_temp"
ATTR_DEWPOINT = "dewpoint"


MAGNUS_K2 = 17.62
MAGNUS_K3 = 243.12

PLATFORM_SCHEMA = SENSOR_PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_INDOOR_TEMP): cv.entity_id,
        vol.Required(CONF_OUTDOOR_TEMP): cv.entity_id,
        vol.Required(CONF_INDOOR_HUMIDITY): cv.entity_id,
        vol.Optional(CONF_CALIBRATION_FACTOR): vol.Coerce(float),
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    }
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up MoldIndicator sensor."""
    name: str = config.get(CONF_NAME, DEFAULT_NAME)
    indoor_temp_sensor: str = config[CONF_INDOOR_TEMP]
    outdoor_temp_sensor: str = config[CONF_OUTDOOR_TEMP]
    indoor_humidity_sensor: str = config[CONF_INDOOR_HUMIDITY]
    calib_factor: float = config[CONF_CALIBRATION_FACTOR]

    async_add_entities(
        [
            MoldIndicator(
                name,
                hass.config.units is METRIC_SYSTEM,
                indoor_temp_sensor,
                outdoor_temp_sensor,
                indoor_humidity_sensor,
                calib_factor,
            )
        ],
        False,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Mold indicator sensor entry."""
    name: str = entry.options[CONF_NAME]
    indoor_temp_sensor: str = entry.options[CONF_INDOOR_TEMP]
    outdoor_temp_sensor: str = entry.options[CONF_OUTDOOR_TEMP]
    indoor_humidity_sensor: str = entry.options[CONF_INDOOR_HUMIDITY]
    calib_factor: float = entry.options[CONF_CALIBRATION_FACTOR]

    async_add_entities(
        [
            MoldIndicator(
                name,
                hass.config.units is METRIC_SYSTEM,
                indoor_temp_sensor,
                outdoor_temp_sensor,
                indoor_humidity_sensor,
                calib_factor,
            )
        ],
        False,
    )


class MoldIndicator(SensorEntity):
    """Represents a MoldIndication sensor."""

    _attr_should_poll = False
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        name: str,
        is_metric: bool,
        indoor_temp_sensor: str,
        outdoor_temp_sensor: str,
        indoor_humidity_sensor: str,
        calib_factor: float,
    ) -> None:
        """Initialize the sensor."""
        self._state: str | None = None
        self._attr_name = name
        self._indoor_temp_sensor = indoor_temp_sensor
        self._indoor_humidity_sensor = indoor_humidity_sensor
        self._outdoor_temp_sensor = outdoor_temp_sensor
        self._calib_factor = calib_factor
        self._is_metric = is_metric
        self._attr_available = False
        self._entities = {
            indoor_temp_sensor,
            indoor_humidity_sensor,
            outdoor_temp_sensor,
        }

        self._dewpoint: float | None = None
        self._indoor_temp: float | None = None
        self._outdoor_temp: float | None = None
        self._indoor_hum: float | None = None
        self._crit_temp: float | None = None

    async def async_added_to_hass(self) -> None:
        """Register callbacks."""

        @callback
        def mold_indicator_sensors_state_listener(
            event: Event[EventStateChangedData],
        ) -> None:
            """Handle for state changes for dependent sensors."""
            new_state = event.data["new_state"]
            old_state = event.data["old_state"]
            entity = event.data["entity_id"]
            _LOGGER.debug(
                "Sensor state change for %s that had old state %s and new state %s",
                entity,
                old_state,
                new_state,
            )

            if self._update_sensor(entity, old_state, new_state):
                self.async_schedule_update_ha_state(True)

        @callback
        def mold_indicator_startup(event: Event) -> None:
            """Add listeners and get 1st state."""
            _LOGGER.debug("Startup for %s", self.entity_id)

            async_track_state_change_event(
                self.hass, list(self._entities), mold_indicator_sensors_state_listener
            )

            # Read initial state
            indoor_temp = self.hass.states.get(self._indoor_temp_sensor)
            outdoor_temp = self.hass.states.get(self._outdoor_temp_sensor)
            indoor_hum = self.hass.states.get(self._indoor_humidity_sensor)

            schedule_update = self._update_sensor(
                self._indoor_temp_sensor, None, indoor_temp
            )

            schedule_update = (
                False
                if not self._update_sensor(
                    self._outdoor_temp_sensor, None, outdoor_temp
                )
                else schedule_update
            )

            schedule_update = (
                False
                if not self._update_sensor(
                    self._indoor_humidity_sensor, None, indoor_hum
                )
                else schedule_update
            )

            if schedule_update:
                self.async_schedule_update_ha_state(True)

        self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_START, mold_indicator_startup
        )

    def _update_sensor(
        self, entity: str, old_state: State | None, new_state: State | None
    ) -> bool:
        """Update information based on new sensor states."""
        _LOGGER.debug("Sensor update for %s", entity)
        if new_state is None:
            return False

        # If old_state is not set and new state is unknown then it means
        # that the sensor just started up
        if old_state is None and new_state.state == STATE_UNKNOWN:
            return False

        if entity == self._indoor_temp_sensor:
            self._indoor_temp = self._update_temp_sensor(new_state)
        elif entity == self._outdoor_temp_sensor:
            self._outdoor_temp = self._update_temp_sensor(new_state)
        elif entity == self._indoor_humidity_sensor:
            self._indoor_hum = self._update_hum_sensor(new_state)

        return True

    @staticmethod
    def _update_temp_sensor(state: State) -> float | None:
        """Parse temperature sensor value."""
        _LOGGER.debug("Updating temp sensor with value %s", state.state)

        # Return an error if the sensor change its state to Unknown.
        if state.state == STATE_UNKNOWN:
            _LOGGER.error(
                "Unable to parse temperature sensor %s with state: %s",
                state.entity_id,
                state.state,
            )
            return None

        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)

        if (temp := util.convert(state.state, float)) is None:
            _LOGGER.error(
                "Unable to parse temperature sensor %s with state: %s",
                state.entity_id,
                state.state,
            )
            return None

        # convert to celsius if necessary
        if unit == UnitOfTemperature.FAHRENHEIT:
            return TemperatureConverter.convert(
                temp, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS
            )
        if unit == UnitOfTemperature.CELSIUS:
            return temp
        _LOGGER.error(
            "Temp sensor %s has unsupported unit: %s (allowed: %s, %s)",
            state.entity_id,
            unit,
            UnitOfTemperature.CELSIUS,
            UnitOfTemperature.FAHRENHEIT,
        )

        return None

    @staticmethod
    def _update_hum_sensor(state: State) -> float | None:
        """Parse humidity sensor value."""
        _LOGGER.debug("Updating humidity sensor with value %s", state.state)

        # Return an error if the sensor change its state to Unknown.
        if state.state == STATE_UNKNOWN:
            _LOGGER.error(
                "Unable to parse humidity sensor %s, state: %s",
                state.entity_id,
                state.state,
            )
            return None

        if (hum := util.convert(state.state, float)) is None:
            _LOGGER.error(
                "Unable to parse humidity sensor %s, state: %s",
                state.entity_id,
                state.state,
            )
            return None

        if (unit := state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)) != PERCENTAGE:
            _LOGGER.error(
                "Humidity sensor %s has unsupported unit: %s %s",
                state.entity_id,
                unit,
                " (allowed: %)",
            )
            return None

        if hum > 100 or hum < 0:
            _LOGGER.error(
                "Humidity sensor %s is out of range: %s %s",
                state.entity_id,
                hum,
                "(allowed: 0-100%)",
            )
            return None

        return hum

    async def async_update(self) -> None:
        """Calculate latest state."""
        _LOGGER.debug("Update state for %s", self.entity_id)
        # check all sensors
        if None in (self._indoor_temp, self._indoor_hum, self._outdoor_temp):
            self._attr_available = False
            self._dewpoint = None
            self._crit_temp = None
            return

        # re-calculate dewpoint and mold indicator
        self._calc_dewpoint()
        self._calc_moldindicator()
        if self._state is None:
            self._attr_available = False
            self._dewpoint = None
            self._crit_temp = None
        else:
            self._attr_available = True

    def _calc_dewpoint(self) -> None:
        """Calculate the dewpoint for the indoor air."""
        # Use magnus approximation to calculate the dew point
        if TYPE_CHECKING:
            assert self._indoor_temp and self._indoor_hum
        alpha = MAGNUS_K2 * self._indoor_temp / (MAGNUS_K3 + self._indoor_temp)
        beta = MAGNUS_K2 * MAGNUS_K3 / (MAGNUS_K3 + self._indoor_temp)

        if self._indoor_hum == 0:
            self._dewpoint = -50  # not defined, assume very low value
        else:
            self._dewpoint = (
                MAGNUS_K3
                * (alpha + math.log(self._indoor_hum / 100.0))
                / (beta - math.log(self._indoor_hum / 100.0))
            )
        _LOGGER.debug("Dewpoint: %f %s", self._dewpoint, UnitOfTemperature.CELSIUS)

    def _calc_moldindicator(self) -> None:
        """Calculate the humidity at the (cold) calibration point."""
        if TYPE_CHECKING:
            assert self._outdoor_temp and self._indoor_temp and self._dewpoint

        if None in (self._dewpoint, self._calib_factor) or self._calib_factor == 0:
            _LOGGER.debug(
                "Invalid inputs - dewpoint: %s, calibration-factor: %s",
                self._dewpoint,
                self._calib_factor,
            )
            self._state = None
            self._attr_available = False
            self._crit_temp = None
            return

        # first calculate the approximate temperature at the calibration point
        self._crit_temp = (
            self._outdoor_temp
            + (self._indoor_temp - self._outdoor_temp) / self._calib_factor
        )

        _LOGGER.debug(
            "Estimated Critical Temperature: %f %s",
            self._crit_temp,
            UnitOfTemperature.CELSIUS,
        )

        # Then calculate the humidity at this point
        alpha = MAGNUS_K2 * self._crit_temp / (MAGNUS_K3 + self._crit_temp)
        beta = MAGNUS_K2 * MAGNUS_K3 / (MAGNUS_K3 + self._crit_temp)

        crit_humidity = (
            math.exp(
                (self._dewpoint * beta - MAGNUS_K3 * alpha)
                / (self._dewpoint + MAGNUS_K3)
            )
            * 100.0
        )

        # check bounds and format
        if crit_humidity > 100:
            self._state = "100"
        elif crit_humidity < 0:
            self._state = "0"
        else:
            self._state = f"{int(crit_humidity):d}"

        _LOGGER.debug("Mold indicator humidity: %s", self._state)

    @property
    def native_value(self) -> StateType:
        """Return the state of the entity."""
        return self._state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        if self._is_metric:
            convert_to = UnitOfTemperature.CELSIUS
        else:
            convert_to = UnitOfTemperature.FAHRENHEIT

        dewpoint = (
            TemperatureConverter.convert(
                self._dewpoint, UnitOfTemperature.CELSIUS, convert_to
            )
            if self._dewpoint is not None
            else None
        )

        crit_temp = (
            TemperatureConverter.convert(
                self._crit_temp, UnitOfTemperature.CELSIUS, convert_to
            )
            if self._crit_temp is not None
            else None
        )

        return {
            ATTR_DEWPOINT: round(dewpoint, 2) if dewpoint else None,
            ATTR_CRITICAL_TEMP: round(crit_temp, 2) if crit_temp else None,
        }
