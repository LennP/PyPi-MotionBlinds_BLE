"""Device for MotionBlinds BLE."""

from __future__ import annotations

import logging
from asyncio import (
    Event,
    FIRST_COMPLETED,
    Future,
    Task,
    TimerHandle,
    create_task,
    get_event_loop,
    sleep,
    wait,
)
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from time import time, time_ns
from typing import Any, Union

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakNotFoundError,
    BleakOutOfConnectionSlotsError,
    establish_connection,
)

from .const import (
    DISPLAY_NAME,
    EXCEPTION_NO_END_POSITIONS,
    EXCEPTION_NO_FAVORITE_POSITION,
    EXCEPTION_NOT_CALIBRATED,
    SETTING_CALIBRATION_DISCONNECT_TIME,
    SETTING_DISCONNECT_TIME,
    SETTING_MAX_COMMAND_ATTEMPTS,
    SETTING_MAX_CONNECT_ATTEMPTS,
    SETTING_NOTIFICATION_DELAY,
    SETTING_DISABLE_CONNECT_STATUS_CALLBACK_TIME,
    MotionBlindType,
    MotionCalibrationType,
    MotionCallback,
    MotionCharacteristic,
    MotionCommandType,
    MotionConnectionType,
    MotionNotificationType,
    MotionRunningType,
    MotionSpeedLevel,
)
from .crypt import MotionCrypt

_LOGGER = logging.getLogger(__name__)


def requires_connection(
    func: Callable | None = None,
    *,
    disable_callback: MotionCallback | None = None,
):
    """Decorate a function making it require end positions."""

    def _requires_connection_decorator(
        func: Callable, disable_callback: MotionCallback | None
    ) -> Callable:
        async def wrapper(self: MotionDevice, *args, **kwargs):
            if not await self.connect(
                disable_callbacks=(
                    [disable_callback] if disable_callback is not None else []
                )
            ):
                return False
            return await func(self, *args, **kwargs)

        return wrapper

    if func is None:

        def decorator(func):
            return _requires_connection_decorator(
                func, disable_callback=disable_callback
            )

        return decorator
    return _requires_connection_decorator(
        func, disable_callback=disable_callback
    )


def requires_end_positions(
    func: Callable | None = None, *, can_calibrate_curtain: bool = False
):
    """Decorate a function making it require end positions."""

    def _requires_end_positions_decorator(
        func: Callable, can_calibrate_curtain: bool
    ) -> Callable:
        async def wrapper(self: MotionDevice, *args, **kwargs):
            # pylint: disable=protected-access
            if self._end_position_info is None:
                # Wait for end position info to be set by notification
                await self._received_end_position_info_event.wait()

            if self.blind_type is MotionBlindType.CURTAIN:
                # Curtain blinds can auto-calibrate and find end positions
                # on some commands that have can_calibrate_curtain set
                if (
                    can_calibrate_curtain
                    and self._calibration_type
                    is MotionCalibrationType.UNCALIBRATED
                ):
                    self.refresh_disconnect_timer(
                        SETTING_CALIBRATION_DISCONNECT_TIME
                    )
                    self.update_calibration(MotionCalibrationType.CALIBRATING)
                    # Continue with the command, will start auto-calibration
            elif (
                self._end_position_info is not None
                and not self._end_position_info.up
            ):
                self.refresh_disconnect_timer()
                if self.blind_type is MotionBlindType.VERTICAL:
                    # Vertical blinds require calibration in mobile app
                    raise NotCalibratedException(
                        EXCEPTION_NOT_CALIBRATED.format(
                            display_name=self.display_name
                        )
                    )
                # If no end positions are set an exception is raised
                self.update_running(MotionRunningType.STILL)
                raise NoEndPositionsException(
                    EXCEPTION_NO_END_POSITIONS.format(
                        display_name=self.display_name
                    )
                )
            return await func(
                self,
                *args,
                **kwargs,
            )

        return wrapper

    if func is None:

        def decorator(func):
            return _requires_end_positions_decorator(
                func, can_calibrate_curtain=can_calibrate_curtain
            )

        return decorator
    return _requires_end_positions_decorator(
        func, can_calibrate_curtain=can_calibrate_curtain
    )


def requires_favorite_position(func: Callable) -> Callable:
    """Decorate a function making it require a favorite position."""

    async def wrapper(self: MotionDevice, *args, **kwargs):
        # pylint: disable=protected-access
        if (
            self._end_position_info is not None
            and not self._end_position_info.favorite
        ):
            self.refresh_disconnect_timer()
            # If no favorite position is set an exception is raised
            self.update_running(MotionRunningType.STILL)
            raise NoFavoritePositionException(
                EXCEPTION_NO_FAVORITE_POSITION.format(
                    display_name=self.display_name
                )
            )
        return await func(self, *args, **kwargs)

    return wrapper


@dataclass
class MotionPositionInfo:
    """Information on whether end positions and favorite position are set."""

    up: bool
    down: bool
    favorite: bool | None = None

    def __init__(
        self, end_positions_byte: int, favorite_bytes: int | None = None
    ) -> None:
        """Initialize the MotionPositionInfo."""
        self.up = bool(end_positions_byte & 0x08)
        self.down = bool(end_positions_byte & 0x04)
        if favorite_bytes is not None:
            self.favorite = bool(favorite_bytes & 0x8000)

    def update_end_positions(self, end_positions_byte: int):
        """Update the end positions."""
        self.up = bool(end_positions_byte & 0x08)
        self.down = bool(end_positions_byte & 0x04)


class ConnectionQueue:
    """Class used to ensure the first caller connects,
    but the last caller's command goes through after connection."""

    _ha_create_task: Callable[[Coroutine], Task] | None = None
    _connection_task: Task | Any | None = None
    _last_caller_cancel: Future | None = None

    def set_ha_create_task(
        self, ha_create_task: Callable[[Coroutine], Task]
    ) -> None:
        """Set the Home Assistant create_task function."""
        self._ha_create_task = ha_create_task

    def _create_connection_task(self, device: MotionDevice) -> Task | Any:
        """Create a connection task."""
        if self._ha_create_task:
            _LOGGER.debug(
                "(%s) Connecting using Home Assistant",
                device.ble_device.address,
            )
            return self._ha_create_task(
                target=device.establish_connection()
            )  # type: ignore[call-arg]
        _LOGGER.debug("(%s) Connecting", device.ble_device.address)
        return get_event_loop().create_task(device.establish_connection())

    async def wait_for_connection(self, device: MotionDevice) -> bool:
        """Wait for a connection, return True to last caller if connected."""
        if self._connection_task is None:
            self._connection_task = self._create_connection_task(device)
        else:
            _LOGGER.debug(
                "(%s) Already connecting, waiting for connection",
                device.ble_device.address,
            )

        # Cancel the previous caller
        if self._last_caller_cancel:
            self._last_caller_cancel.set_result(True)
        self._last_caller_cancel = Future()

        try:
            done: set[Union[Task, Future]]
            done, _ = await wait(
                [self._connection_task, self._last_caller_cancel],
                return_when=FIRST_COMPLETED,
            )
            if self._connection_task in done:
                result = (
                    self._connection_task.result()
                )  # Get the result of the completed connection task
                self._connection_task = None  # Reset the connection task
                return result
            return False

        except (BleakOutOfConnectionSlotsError, BleakNotFoundError) as e:
            device.update_connection(MotionConnectionType.DISCONNECTED)
            self._connection_task = None
            raise e

    def cancel(self) -> bool:
        """Cancel the connection task."""
        if self._connection_task is not None:
            # Indicate the connection has failed
            self._connection_task.cancel()
            self._connection_task = None
            return True
        return False


class MotionDevice:
    """Class used to control a MotionBlinds BLE device."""

    ble_device: BLEDevice
    blind_type: MotionBlindType
    display_name: str
    rssi: int | None

    # Connection
    _connection_queue: ConnectionQueue
    _current_bleak_client: BleakClient | None

    # States
    _calibration_type: MotionCalibrationType | None
    _connection_type: MotionConnectionType
    _position: int | None
    _tilt: int | None
    _battery: int | None
    _speed: int | None
    _end_position_info: MotionPositionInfo | None
    _received_end_position_info_event: Event

    # Disconnection
    _disconnect_time: float | None
    _disconnect_timer: TimerHandle | Callable | None

    # Callbacks that are used to interface with HA
    _ha_call_later: Callable[[int, Coroutine], Callable] | None = None

    # Callbacks
    _connect_status_query_time: float | None
    _disabled_connection_callbacks: list[MotionCallback]
    _status_callbacks: list[
        Callable[
            [int, int, int, MotionSpeedLevel | None, MotionPositionInfo], None
        ]
    ]
    _feedback_callbacks: list[Callable[[int, int, MotionPositionInfo], None]]
    _position_callbacks: list[Callable[[int, int], None]]
    _battery_callbacks: list[Callable[[int], None]]
    _speed_callbacks: list[Callable[[MotionSpeedLevel | None], None]]
    _end_position_callbacks: list[Callable[[MotionSpeedLevel | None], None]]
    _connection_callbacks: list[Callable[[MotionConnectionType], None]]
    _calibration_callbacks: list[
        Callable[[MotionCalibrationType | None], None]
    ]
    _running_callbacks: list[Callable[[MotionRunningType], None]]
    _signal_strength_callbacks: list[Callable[[int], None]]

    def __init__(
        self,
        device: str | BLEDevice,
        blind_type: MotionBlindType = MotionBlindType.ROLLER,
        rssi: int | None = None,
    ) -> None:
        """Initialize the MotionDevice."""
        self.blind_type = blind_type
        self.rssi = rssi

        if isinstance(device, BLEDevice):
            self.ble_device = device
        else:
            _LOGGER.warning(
                "(%s) Could not find BLEDevice,"
                " creating new BLEDevice from address",
                device,
            )
            self.ble_device = BLEDevice(device, device, {}, rssi=None)

        self.display_name = DISPLAY_NAME.format(
            mac_code=self.ble_device.address.replace(":", "")[-4:]
        )

        self._received_end_position_info_event = Event()

        self._position: int = None
        self._tilt: int = None
        self._calibration_type: MotionCalibrationType | None = None
        self._end_position_info: MotionPositionInfo | None = None
        self._current_bleak_client: BleakClient | None = None
        self._connection_type: MotionConnectionType = (
            MotionConnectionType.DISCONNECTED
        )

        self._disconnect_time: float | None = None
        self._disconnect_timer: TimerHandle | Callable | None = None

        self._connection_queue = ConnectionQueue()

        self._connect_status_query_time = None
        self._disabled_connection_callbacks = []
        self._status_callbacks = []
        self._feedback_callbacks = []
        self._position_callbacks = []
        self._battery_callbacks = []
        self._speed_callbacks = []
        self._end_position_callbacks = []
        self._connection_callbacks = []
        self._calibration_callbacks = []
        self._running_callbacks = []
        self._signal_strength_callbacks = []

    def set_ble_device(
        self, ble_device: BLEDevice, rssi: int | None = None
    ) -> None:
        """Set the BLEDevice for this device."""
        self.ble_device = ble_device
        self.update_signal_strength(rssi)

    @property
    def connection_type(self) -> MotionConnectionType:
        """Return the connection type."""
        return self._connection_type

    def set_ha_create_task(
        self, ha_create_task: Callable[[Coroutine], Task]
    ) -> None:
        """Set the create_task function to use."""
        self._connection_queue.set_ha_create_task(ha_create_task)

    def set_ha_call_later(
        self, ha_call_later: Callable[[int, Coroutine], Callable]
    ) -> None:
        """Set the call_later function to use."""
        self._ha_call_later = ha_call_later

    def cancel_disconnect_timer(self) -> None:
        """Cancel the disconnect timeout."""
        if self._disconnect_timer:
            # Cancel current timeout
            if callable(self._disconnect_timer):
                self._disconnect_timer()
            else:
                self._disconnect_timer.cancel()

    def refresh_disconnect_timer(
        self, timeout: int | None = None, force: bool = False
    ) -> None:
        """Refresh the time before the device is disconnected."""
        timeout = SETTING_DISCONNECT_TIME if timeout is None else timeout
        # Don't refresh if existing timeout > timeout unless forced
        new_disconnect_time = time_ns() // 1e6 + timeout * 1e3
        if (
            not force
            and self._disconnect_timer is not None
            and self._disconnect_time is not None
            and self._disconnect_time > new_disconnect_time
        ):
            return

        self.cancel_disconnect_timer()

        async def _disconnect_later(_: datetime | None = None):
            _LOGGER.debug(
                "(%s) Disconnecting after %is",
                self.ble_device.address,
                timeout,
            )
            await self.disconnect()

        self._disconnect_time = new_disconnect_time
        if self._ha_call_later:
            _LOGGER.debug(
                "(%s) Refreshing disconnect timeout to %i"
                " using Home Assistant",
                self.ble_device.address,
                timeout,
            )
            self._disconnect_timer = self._ha_call_later(
                delay=timeout, action=_disconnect_later
            )  # type: ignore[call-arg]
        else:
            _LOGGER.debug(
                "(%s) Refreshing disconnect timeout to %is",
                self.ble_device.address,
                timeout,
            )
            self._disconnect_timer = get_event_loop().call_later(
                timeout, create_task, _disconnect_later()
            )

    def _notification_callback(
        self, _: BleakGATTCharacteristic, byte_array: bytearray
    ) -> None:
        """Handle a received notification."""
        decrypted_message: str = MotionCrypt.decrypt(byte_array.hex())
        decrypted_message_bytes: bytes = byte_array.fromhex(decrypted_message)
        _LOGGER.debug(
            "(%s) Received message: %s",
            self.ble_device.address,
            decrypted_message,
        )

        if decrypted_message.startswith(MotionNotificationType.POSITION.value):
            position: int = decrypted_message_bytes[6]
            tilt: int = decrypted_message_bytes[7]
            end_position_info = (
                self._end_position_info
                if self._end_position_info is not None
                else MotionPositionInfo(decrypted_message_bytes[4])
            )
            end_position_info.update_end_positions(decrypted_message_bytes[4])
            self.update_position(position, tilt)
            self.update_end_position_info(end_position_info)
            self.update_running(MotionRunningType.STILL)
            for feedback_callback in self._feedback_callbacks:
                feedback_callback(
                    position,
                    tilt,
                    end_position_info,
                )
            _LOGGER.debug(
                (
                    "(%s) Received feedback; position: %s, tilt: %s, "
                    "top position set: %s, bottom position set: %s, "
                    "favorite position set: %s"
                ),
                self.ble_device.address,
                str(position),
                str(tilt),
                end_position_info.up,
                end_position_info.down,
                end_position_info.favorite,
            )
        elif decrypted_message.startswith(MotionNotificationType.STATUS.value):
            position: int = decrypted_message_bytes[6]
            tilt: int = decrypted_message_bytes[7]
            battery_percentage: int = decrypted_message_bytes[17]
            end_position_info = MotionPositionInfo(
                decrypted_message_bytes[4],
                int.from_bytes(
                    [
                        decrypted_message_bytes[14],
                        decrypted_message_bytes[15],
                    ],
                    "little",
                ),
            )
            try:
                speed_level: MotionSpeedLevel | None = MotionSpeedLevel(
                    decrypted_message_bytes[12]
                )
            except ValueError:
                speed_level = None
            self.update_position(position, tilt)
            self.update_battery(battery_percentage)
            self.update_end_position_info(end_position_info)
            self.update_speed(speed_level)
            for status_callback in self._status_callbacks:
                status_callback(
                    position,
                    tilt,
                    battery_percentage,
                    speed_level,
                    end_position_info,
                )
            _LOGGER.debug(
                (
                    "(%s) Received status; position: %s, tilt: %s, "
                    "speed: %s, top position set: %s, battery: %s, "
                    "bottom position set: %s, favorite position set: %s"
                ),
                self.ble_device.address,
                str(position),
                str(tilt),
                str(battery_percentage),
                speed_level.name if speed_level is not None else None,
                end_position_info.up,
                end_position_info.down,
                end_position_info.favorite,
            )

    def _disconnect_callback(self, _: BleakClient) -> None:
        """Handle a BleakClient disconnect."""
        _LOGGER.debug("(%s) Disconnected", self.ble_device.address)
        self.update_calibration(None)
        self.update_running(None)
        self.update_speed(None)
        self.update_connection(MotionConnectionType.DISCONNECTED)
        self._current_bleak_client = None

    async def connect(
        self, disable_callbacks: list[MotionCallback] | None = None
    ) -> bool:
        """Connect to the device if not connected.

        Return whether or not the motor is ready for a command.
        """
        if not self.is_connected():
            # Connect if not connected yet and not busy connecting
            self._disable_connection_callbacks(
                disable_callbacks if disable_callbacks is not None else []
            )
            self._received_end_position_info_event.clear()
            return await self._connection_queue.wait_for_connection(self)
        self.refresh_disconnect_timer()
        return True

    async def disconnect(self) -> None:
        """Disconnect from the device."""
        self.update_connection(MotionConnectionType.DISCONNECTING)
        self.cancel_disconnect_timer()
        if self._connection_queue.cancel():
            _LOGGER.debug("(%s) Cancelled connecting", self.ble_device.address)
        if self._current_bleak_client is not None:
            _LOGGER.debug("(%s) Disconnecting", self.ble_device.address)
            await self._current_bleak_client.disconnect()
            _LOGGER.debug("(%s) Disconnected", self.ble_device.address)
            self._current_bleak_client = None
        else:
            self.update_connection(MotionConnectionType.DISCONNECTED)

    async def establish_connection(self) -> bool:
        """Connect to device, return whether motor is ready for a command."""
        if self._connection_type is MotionConnectionType.CONNECTING:
            return False

        self.update_connection(MotionConnectionType.CONNECTING)

        bleak_client = await establish_connection(
            BleakClient,
            self.ble_device,
            self.ble_device.address,
            max_attempts=SETTING_MAX_CONNECT_ATTEMPTS,
        )

        self._current_bleak_client = bleak_client
        self.update_connection(MotionConnectionType.CONNECTED)

        await bleak_client.start_notify(
            str(MotionCharacteristic.NOTIFICATION.value),
            self._notification_callback,
        )

        # Used to initialize
        await self.set_key()

        if self.blind_type in [
            MotionBlindType.CURTAIN,
            MotionBlindType.VERTICAL,
        ]:
            _LOGGER.warning("Using notification delay")
            await sleep(SETTING_NOTIFICATION_DELAY)

        # Set the point (used after calibrating Curtain)
        # await self.point_set_query()

        self._connect_status_query_time = time_ns()
        await self.status_query()

        bleak_client.set_disconnected_callback(self._disconnect_callback)
        self.refresh_disconnect_timer()

        return True

    def is_connected(self) -> bool:
        """Return whether or not the device is connected."""
        return (
            self._current_bleak_client is not None
            and self._current_bleak_client.is_connected
        )

    async def _send_command(
        self, command_prefix: str, connection_command: bool = False
    ) -> bool:
        """Write a message to the command characteristic.

        Return whether the command was successfully executed.
        """
        # Command must be generated just before sending due get_time timing
        command = MotionCrypt.encrypt(command_prefix + MotionCrypt.get_time())
        _LOGGER.debug(
            "(%s) Sending message: %s",
            self.ble_device.address,
            MotionCrypt.decrypt(command),
        )
        # response=False to solve Unlikely Error: [org.bluez.Error.Failed]
        # Operation failed with ATT error: 0x0e (Unlikely Error)
        # response=True: 0.20s, response=False: 0.0005s
        number_of_tries = 0
        while number_of_tries < SETTING_MAX_COMMAND_ATTEMPTS:
            try:
                if self._current_bleak_client is not None:
                    before_time = time()
                    await self._current_bleak_client.write_gatt_char(
                        str(MotionCharacteristic.COMMAND.value),
                        bytes.fromhex(command),
                        response=True,
                    )
                    after_time = time()
                    _LOGGER.debug(
                        "(%s) Received response in %.2fs",
                        self.ble_device.address,
                        after_time - before_time,
                    )
                    return True
                return False
            except BleakError as e:
                _LOGGER.warning(
                    "(%s) Could not send message (try #%i): %s",
                    self.ble_device.address,
                    number_of_tries,
                    e,
                )
                number_of_tries += 1
        return False

    @requires_connection
    async def user_query(self) -> bool:
        """Send user_query command."""
        command_prefix = str(MotionCommandType.USER_QUERY.value)
        return await self._send_command(
            command_prefix, connection_command=True
        )

    @requires_connection
    async def set_key(self) -> bool:
        """Send set_key command."""
        command_prefix = str(MotionCommandType.SET_KEY.value)
        return await self._send_command(
            command_prefix, connection_command=True
        )

    @requires_connection
    async def status_query(self) -> bool:
        """Send status_query command."""
        command_prefix = str(MotionCommandType.STATUS_QUERY.value)
        return await self._send_command(
            command_prefix, connection_command=True
        )

    @requires_connection
    async def point_set_query(self) -> bool:
        """Send point_set_query command."""
        command_prefix = str(MotionCommandType.POINT_SET_QUERY.value)
        return await self._send_command(
            command_prefix, connection_command=True
        )

    @requires_connection(disable_callback=MotionCallback.SPEED)
    async def speed(self, speed_level: MotionSpeedLevel) -> bool:
        """Change the speed level of the device."""
        command_prefix = str(MotionCommandType.SPEED.value) + hex(
            int(speed_level.value)
        )[2:].zfill(2)
        return await self._send_command(command_prefix)

    @requires_connection(disable_callback=MotionCallback.POSITION)
    @requires_end_positions(can_calibrate_curtain=True)
    async def position(self, position: int) -> bool:
        """Move the device to a specific position,
        0 is fully open, 100 is fully closed."""
        self.update_running(
            MotionRunningType.UNKNOWN
            if self._position is None
            else (
                MotionRunningType.STILL
                if position == self._position
                else (
                    MotionRunningType.OPENING
                    if position < self._position
                    else MotionRunningType.CLOSING
                )
            )
        )
        assert not position < 0 and not position > 100
        command_prefix = (
            str(MotionCommandType.PERCENT.value)
            + hex(position)[2:].zfill(2)
            + "00"
        )
        return await self._send_command(command_prefix)

    @requires_connection
    @requires_end_positions(can_calibrate_curtain=True)
    async def open(self) -> bool:
        """Open the device."""
        self.update_running(MotionRunningType.OPENING)
        command_prefix = str(MotionCommandType.OPEN.value)
        return await self._send_command(command_prefix)

    @requires_connection
    @requires_end_positions(can_calibrate_curtain=True)
    async def close(self) -> bool:
        """Close the device."""
        self.update_running(MotionRunningType.CLOSING)
        command_prefix = str(MotionCommandType.CLOSE.value)
        return await self._send_command(command_prefix)

    @requires_connection
    @requires_end_positions
    async def stop(self) -> bool:
        """Stop moving the device."""
        command_prefix = str(MotionCommandType.STOP.value)
        return await self._send_command(command_prefix)

    @requires_connection
    @requires_end_positions
    @requires_favorite_position
    async def favorite(self) -> bool:
        """Move the device to the favorite position."""
        self.update_running(MotionRunningType.UNKNOWN)
        command_prefix = str(MotionCommandType.FAVORITE.value)
        return await self._send_command(command_prefix)

    @requires_connection(disable_callback=MotionCallback.POSITION)
    @requires_end_positions
    async def tilt(self, angle: int) -> bool:
        """Tilt the device to a specific angle.
        0 is fully open, 100 is fully closed."""
        assert not angle < 0 and not angle > 180
        self.update_running(
            MotionRunningType.UNKNOWN
            if self._tilt is None
            else (
                MotionRunningType.STILL
                if angle == self._tilt
                else (
                    MotionRunningType.OPENING
                    if angle < self._tilt
                    else MotionRunningType.CLOSING
                )
            )
        )
        command_prefix = (
            str(MotionCommandType.ANGLE.value) + "00" + hex(angle)[2:].zfill(2)
        )
        return await self._send_command(command_prefix)

    @requires_connection
    @requires_end_positions
    async def open_tilt(self) -> bool:
        """Tilt the device open."""
        self.update_running(MotionRunningType.OPENING)
        command_prefix = (
            str(MotionCommandType.ANGLE.value) + "00" + hex(0)[2:].zfill(2)
        )
        return await self._send_command(command_prefix)

    @requires_connection
    @requires_end_positions
    async def close_tilt(self) -> bool:
        """Tilt the device closed."""
        self.update_running(MotionRunningType.CLOSING)
        command_prefix = (
            str(MotionCommandType.ANGLE.value) + "00" + hex(180)[2:].zfill(2)
        )
        return await self._send_command(command_prefix)

    def _disable_connection_callbacks(
        self, callbacks: list[MotionCallback]
    ) -> None:
        self._disabled_connection_callbacks = callbacks

    def _is_connection_callback_disabled(
        self, callback: MotionCallback
    ) -> bool:
        return (
            callback in self._disabled_connection_callbacks
            and self._connect_status_query_time is not None
            and time_ns() - self._connect_status_query_time
            < SETTING_DISABLE_CONNECT_STATUS_CALLBACK_TIME * 1e9
        )

    def update_connection(self, connection_type: MotionConnectionType) -> None:
        """Update the connection to a particular connection type."""
        _LOGGER.debug(
            "(%s) Updating connection: %s",
            self.ble_device.address,
            connection_type.value,
        )
        self._connection_type = connection_type
        if self._is_connection_callback_disabled(MotionCallback.CONNECTION):
            return
        for connection_callback in self._connection_callbacks:
            connection_callback(connection_type)

    def update_calibration(
        self, calibration_type: MotionCalibrationType | None
    ) -> None:
        """Update the calibration to a particular calibration type."""
        _LOGGER.debug(
            "(%s) Updating calibration: %s",
            self.ble_device.address,
            (calibration_type.value if calibration_type is not None else None),
        )
        self._calibration_type = calibration_type
        if self._is_connection_callback_disabled(MotionCallback.CALIBRATION):
            return
        for calibration_callback in self._calibration_callbacks:
            calibration_callback(calibration_type)

    def update_running(
        self, running_type: MotionCalibrationType | None
    ) -> None:
        """Update the running to a particular running type."""
        _LOGGER.debug(
            "(%s) Updating running: %s",
            self.ble_device.address,
            (running_type.value if running_type is not None else None),
        )
        if self._is_connection_callback_disabled(MotionCallback.RUNNING):
            return
        for running_callback in self._running_callbacks:
            running_callback(running_type)

    def update_battery(self, battery_percentage: int | None) -> None:
        """Update the battery percentage."""
        _LOGGER.debug(
            "(%s) Updating battery: %s",
            self.ble_device.address,
            battery_percentage,
        )
        self._battery = battery_percentage
        if self._is_connection_callback_disabled(MotionCallback.BATTERY):
            return
        for battery_callback in self._battery_callbacks:
            battery_callback(battery_percentage)

    def update_speed(self, speed_level: MotionSpeedLevel | None) -> None:
        """Update the speed to a particular speed level."""
        _LOGGER.debug(
            "(%s) Updating speed: %s",
            self.ble_device.address,
            speed_level,
        )
        self._speed = speed_level
        if self._is_connection_callback_disabled(MotionCallback.SPEED):
            return
        for speed_callback in self._speed_callbacks:
            speed_callback(speed_level)

    def update_end_position_info(
        self, end_position_info: MotionPositionInfo | None
    ) -> None:
        """Update the end_position_info."""
        _LOGGER.debug(
            (
                "(%s) Updating end position info; top position set: %s, "
                "bottom position set: %s, favorite position set: %s"
            ),
            self.ble_device.address,
            end_position_info.up,
            end_position_info.down,
            end_position_info.favorite,
        )
        self._end_position_info = end_position_info
        self.update_calibration(
            MotionCalibrationType.CALIBRATED
            if self._end_position_info.up
            else MotionCalibrationType.UNCALIBRATED
        )
        self._received_end_position_info_event.set()
        if self._is_connection_callback_disabled(
            MotionCallback.END_POSITION_INFO
        ):
            return
        for end_position_callback in self._end_position_callbacks:
            end_position_callback(self._end_position_info)

    def update_position(self, position: int | None, tilt: int | None) -> None:
        """Update the position and tilt."""
        _LOGGER.debug(
            "(%s) Updating position: %s, tilt: %s",
            self.ble_device.address,
            str(position),
            str(tilt),
        )
        self._position = position
        self._tilt = tilt
        if self._is_connection_callback_disabled(MotionCallback.POSITION):
            return
        for position_callback in self._position_callbacks:
            position_callback(self._position, self._tilt)

    def update_signal_strength(self, rssi: int | None) -> None:
        """Update the signal strength."""
        _LOGGER.debug(
            "(%s) Updating signal strength: %s",
            self.ble_device.address,
            str(rssi),
        )
        self.rssi = rssi
        if self._is_connection_callback_disabled(
            MotionCallback.SIGNAL_STRENGTH
        ):
            return
        for signal_strength_callback in self._signal_strength_callbacks:
            signal_strength_callback(rssi)

    def register_status_callback(
        self,
        callback: Callable[
            [int, int, int, MotionSpeedLevel | None, MotionPositionInfo], None
        ],
    ) -> None:
        """Register the callback used to update when status is received.
        Includes position, tilt, battery percentage and end position info."""
        self._status_callbacks.append(callback)

    def register_feedback_callback(
        self, callback: Callable[[int, int, MotionPositionInfo], None]
    ) -> None:
        """Register the callback used to update when feedback is received.
        Includes position, tilt, and end position info."""
        self._feedback_callbacks.append(callback)

    def register_position_callback(
        self, callback: Callable[[int, int], None]
    ) -> None:
        """Register the callback used to update the position and tilt."""
        self._position_callbacks.append(callback)

    def register_battery_callback(
        self, callback: Callable[[int], None]
    ) -> None:
        """Register the callback used to update the battery percentage."""
        self._battery_callbacks.append(callback)

    def register_end_position_callback(
        self, callback: Callable[[MotionPositionInfo], None]
    ) -> None:
        """Register the callback used to update the end position info."""
        self._end_position_callbacks.append(callback)

    def register_speed_callback(
        self, callback: Callable[[MotionSpeedLevel | None], None]
    ) -> None:
        """Register the callback used to update the speed level."""
        self._speed_callbacks.append(callback)

    def register_connection_callback(
        self, callback: Callable[[MotionConnectionType], None]
    ) -> None:
        """Register the callback used to update the connection status."""
        self._connection_callbacks.append(callback)

    def register_calibration_callback(
        self, callback: Callable[[MotionCalibrationType], None]
    ) -> None:
        """Register the callback used to update the calibration status."""
        self._calibration_callbacks.append(callback)

    def register_running_callback(
        self, callback: Callable[[MotionRunningType], None]
    ) -> None:
        """Register the callback used to update the running type."""
        self._running_callbacks.append(callback)

    def register_signal_strength_callback(
        self, callback: Callable[[int], None]
    ) -> None:
        """Register the callback used to update the signal strength."""
        self._signal_strength_callbacks.append(callback)


class NoEndPositionsException(Exception):
    """Exception to indicate the blind's endpositions must be set."""


class NoFavoritePositionException(Exception):
    """Exception to indicate the blind's favorite must be set."""


class NotCalibratedException(Exception):
    """Exception to indicate the blind is not calibrated."""
