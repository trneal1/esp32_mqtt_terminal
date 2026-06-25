"""
mqtt_tft_terminal.py
====================
Python client library for the ESP8266 MQTT TFT Terminal firmware.

The public drawing API mirrors ``tft_terminal.py`` from the TCP firmware, but
commands are published as JSON over MQTT.  Normal drawing commands are
fire-and-forget.  ``ping()``, ``query()``, and ``set_rotation()`` wait for the
firmware's status-topic response.

Install dependency:

    pip install paho-mqtt

Example:

    from mqtt_tft_terminal import MQTTTFTTerminal

    with MQTTTFTTerminal("192.168.1.10", command_topic="weather") as tft:
        tft.set_channels(["weather"], retain=True)
        tft.sync()
        tft.fill_screen("#001830")
        tft.text(8, 8, "Hello MQTT", color="cyan", size=2)

Batch several commands into one MQTT payload:

    with tft.batch():
        tft.clear()
        tft.rect(0, 0, tft.width, tft.height, "white")
        tft.text(8, 8, "One MQTT message")
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Condition
from typing import Deque, Dict, Generator, Iterable, List, Optional, Tuple, Union

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover - exercised only when dependency absent
    mqtt = None  # type: ignore[assignment]


ColorSpec = Union[str, Tuple[int, int, int], int]


class TFTError(Exception):
    """Raised for invalid firmware status, MQTT failures, or timeouts."""


@dataclass(frozen=True)
class DisplayProfile:
    driver: str
    portrait_w: int
    portrait_h: int

    def dimensions_for_rotation(self, rotation: int) -> Tuple[int, int]:
        if rotation in (1, 3):
            return self.portrait_h, self.portrait_w
        return self.portrait_w, self.portrait_h


DISPLAY_PROFILES: Dict[str, DisplayProfile] = {
    "ili9341": DisplayProfile("ILI9341", 240, 320),
    "st7796": DisplayProfile("ST7796", 320, 480),
    "st7735": DisplayProfile("ST7735", 128, 160),
    "st7735_128": DisplayProfile("ST7735", 128, 128),
}

_DEFAULT_DISPLAY = "ili9341"
_DEFAULT_ROTATION = 3
_HEX_RE = re.compile(r"^#([0-9A-Fa-f]{3}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$")

COLORS: Dict[str, str] = {
    "black": "#000000",
    "white": "#FFFFFF",
    "red": "#FF0000",
    "green": "#00FF00",
    "blue": "#0000FF",
    "yellow": "#FFFF00",
    "cyan": "#00FFFF",
    "magenta": "#FF00FF",
    "orange": "#FF8800",
    "purple": "#880088",
    "pink": "#FF69B4",
    "gold": "#FFD700",
    "silver": "#C0C0C0",
    "navy": "#000080",
    "teal": "#008080",
    "lime": "#00FF80",
    "maroon": "#800000",
    "olive": "#808000",
    "gray": "#808080",
    "grey": "#808080",
    "darkgray": "#404040",
    "darkgrey": "#404040",
}


def _profile_for(name: str) -> DisplayProfile:
    key = name.strip().lower()
    if key not in DISPLAY_PROFILES:
        known = ", ".join(sorted(DISPLAY_PROFILES))
        raise ValueError(f"Unknown display type {name!r}. Known types: {known}")
    return DISPLAY_PROFILES[key]


def _normalise_color(value: ColorSpec, param_name: str = "color") -> Union[str, int]:
    if isinstance(value, int) and not isinstance(value, bool):
        if not 0 <= value <= 0xFFFF:
            raise ValueError(f"Parameter '{param_name}' must be in range 0-65535.")
        return value

    if isinstance(value, (tuple, list)):
        if len(value) != 3:
            raise ValueError(f"Parameter '{param_name}' must be an (r, g, b) triple.")
        r, g, b = value
        for component, label in ((r, "r"), (g, "g"), (b, "b")):
            if not isinstance(component, int) or isinstance(component, bool):
                raise TypeError(f"Parameter '{param_name}.{label}' must be an int.")
            if not 0 <= component <= 255:
                raise ValueError(f"Parameter '{param_name}.{label}' must be 0-255.")
        return f"#{r:02X}{g:02X}{b:02X}"

    if isinstance(value, str):
        lower = value.lower()
        if lower in COLORS:
            return COLORS[lower]
        if not _HEX_RE.match(value):
            names = ", ".join(sorted(COLORS))
            raise ValueError(
                f"Parameter '{param_name}' is not #RGB/#RRGGBB or a known name. "
                f"Known names: {names}"
            )
        digits = value[1:]
        if len(digits) == 3:
            digits = "".join(c * 2 for c in digits)
        elif len(digits) == 8:
            digits = digits[:6]
        return f"#{digits.upper()}"

    raise TypeError(
        f"Parameter '{param_name}' must be a color string, (r,g,b) tuple, or int."
    )


def _check_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"Parameter '{name}' must be an int.")
    return value


def _check_coord(value: object, name: str, maximum: int) -> int:
    v = _check_int(value, name)
    if not 0 <= v < maximum:
        raise ValueError(f"Parameter '{name}'={v} is outside [0, {maximum - 1}].")
    return v


def _check_dim(value: object, name: str) -> int:
    v = _check_int(value, name)
    if v <= 0:
        raise ValueError(f"Parameter '{name}'={v} must be greater than 0.")
    return v


def _check_number(value: object, name: str) -> Union[int, float]:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"Parameter '{name}' must be a number.")
    return value


def _check_size(value: object) -> int:
    v = _check_int(value, "size")
    if not 1 <= v <= 8:
        raise ValueError("Parameter 'size' must be in range [1, 8].")
    return v


def _check_text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("Parameter 'text' must be a str.")
    if not value:
        raise ValueError("Parameter 'text' must not be empty.")
    return value


def _check_topic(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"'{name}' must be a non-empty MQTT topic string.")
    if len(value) >= 64:
        raise ValueError(f"'{name}' must be shorter than 64 characters.")
    return value


class MQTTTFTTerminal:
    """MQTT client for the ESP8266 MQTT TFT Terminal firmware.

    Parameters
    ----------
    broker:
        MQTT broker hostname or IP address.
    command_topic:
        Display channel topic to publish drawing commands to.
    control_topic:
        Topic used to publish the channel list, default ``"tft/control"``.
    status_topic:
        Topic where firmware events are published, default ``"tft/status"``.
    username, password:
        Optional MQTT credentials.
    wait_for_status:
        If true, subscribe to the status topic and wait for query/ping/rotation.
    """

    def __init__(
        self,
        broker: str,
        port: int = 1883,
        *,
        command_topic: str = "tft/default",
        control_topic: str = "tft/control",
        status_topic: str = "tft/status",
        username: str = "",
        password: str = "",
        client_id: Optional[str] = None,
        display: Optional[str] = None,
        rotation: int = _DEFAULT_ROTATION,
        width: Optional[int] = None,
        height: Optional[int] = None,
        qos: int = 0,
        retain: bool = False,
        timeout: float = 5.0,
        auto_connect: bool = True,
        wait_for_status: bool = True,
    ) -> None:
        if not isinstance(broker, str) or not broker:
            raise ValueError("'broker' must be a non-empty string.")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("'port' must be in range [1, 65535].")
        if qos not in (0, 1, 2):
            raise ValueError("'qos' must be 0, 1, or 2.")
        if rotation not in (0, 1, 2, 3):
            raise ValueError("'rotation' must be 0, 1, 2, or 3.")

        self._broker = broker
        self._port = port
        self._command_topic = _check_topic(command_topic, "command_topic")
        self._control_topic = _check_topic(control_topic, "control_topic")
        self._status_topic = _check_topic(status_topic, "status_topic")
        self._qos = qos
        self._retain = bool(retain)
        self._timeout = float(timeout)
        self._wait_for_status = bool(wait_for_status)
        self._connected = False
        self._batch_stack: List[List[dict]] = []
        self._statuses: Deque[dict] = deque(maxlen=100)
        self._status_cv = Condition()

        self._profile = _profile_for(display or _DEFAULT_DISPLAY)
        if width is not None or height is not None:
            if not isinstance(width, int) or width <= 0:
                raise ValueError("'width' must be a positive int.")
            if not isinstance(height, int) or height <= 0:
                raise ValueError("'height' must be a positive int.")
            self._width = width
            self._height = height
        else:
            self._width, self._height = self._profile.dimensions_for_rotation(rotation)

        cid = client_id or f"mqtt-tft-terminal-{uuid.uuid4().hex[:8]}"
        self._client = None
        if mqtt is not None:
            self._client = mqtt.Client(client_id=cid)
            if username:
                self._client.username_pw_set(username, password or None)
            self._client.on_connect = self._on_connect
            self._client.on_disconnect = self._on_disconnect
            self._client.on_message = self._on_message

        if auto_connect:
            self.connect()

    def connect(self) -> None:
        if mqtt is None:
            raise TFTError("Missing dependency: install with 'pip install paho-mqtt'.")
        if self._client is None:
            self._client = mqtt.Client(client_id=f"mqtt-tft-terminal-{uuid.uuid4().hex[:8]}")
            self._client.on_connect = self._on_connect
            self._client.on_disconnect = self._on_disconnect
            self._client.on_message = self._on_message
        if self._connected:
            return
        try:
            self._client.connect(self._broker, self._port, keepalive=30)
            self._client.loop_start()
        except OSError as exc:
            raise TFTError(f"Cannot connect to MQTT broker {self._broker}:{self._port}: {exc}") from exc

        deadline = time.monotonic() + self._timeout
        while not self._connected and time.monotonic() < deadline:
            time.sleep(0.01)
        if not self._connected:
            raise TFTError("Timed out waiting for MQTT connection.")

    def disconnect(self) -> None:
        if self._client is None:
            return
        self._client.loop_stop()
        self._client.disconnect()
        self._connected = False

    close = disconnect

    @property
    def connected(self) -> bool:
        return self._connected

    def __enter__(self) -> "MQTTTFTTerminal":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def command_topic(self) -> str:
        return self._command_topic

    @command_topic.setter
    def command_topic(self, value: str) -> None:
        self._command_topic = _check_topic(value, "command_topic")

    @property
    def profile(self) -> DisplayProfile:
        return self._profile

    def set_display_size(self, width: int, height: int) -> None:
        if not isinstance(width, int) or width <= 0:
            raise ValueError("'width' must be a positive int.")
        if not isinstance(height, int) or height <= 0:
            raise ValueError("'height' must be a positive int.")
        self._width = width
        self._height = height

    def _on_connect(self, client, _userdata, _flags, rc) -> None:
        if rc != 0:
            return
        self._connected = True
        if self._wait_for_status:
            client.subscribe(self._status_topic, qos=self._qos)

    def _on_disconnect(self, _client, _userdata, _rc) -> None:
        self._connected = False

    def _on_message(self, _client, _userdata, msg) -> None:
        if msg.topic != self._status_topic:
            return
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if isinstance(payload, dict):
            with self._status_cv:
                self._statuses.append(payload)
                self._status_cv.notify_all()

    def _publish_json(self, topic: str, payload: Union[dict, List[dict]], *, retain: Optional[bool] = None) -> None:
        if not self._connected:
            raise TFTError("Not connected; call connect() first.")
        if self._client is None or mqtt is None:
            raise TFTError("Missing dependency: install with 'pip install paho-mqtt'.")
        data = json.dumps(payload, separators=(",", ":"))
        info = self._client.publish(
            topic,
            data,
            qos=self._qos,
            retain=self._retain if retain is None else retain,
        )
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise TFTError(f"MQTT publish failed with rc={info.rc}.")
        if self._qos:
            info.wait_for_publish(timeout=self._timeout)
            if not info.is_published():
                raise TFTError("Timed out waiting for MQTT publish acknowledgement.")

    def _send(self, payload: dict) -> None:
        if self._batch_stack:
            self._batch_stack[-1].append(payload)
            return
        self._publish_json(self._command_topic, payload)

    def _request_status(self, payload: dict, event: str) -> dict:
        if not self._wait_for_status:
            raise TFTError("Status waits are disabled for this client.")
        with self._status_cv:
            self._statuses.clear()
        self._publish_json(self._command_topic, payload)
        return self.wait_for_event(event)

    def wait_for_event(self, event: str, *, timeout: Optional[float] = None) -> dict:
        deadline = time.monotonic() + (self._timeout if timeout is None else timeout)
        with self._status_cv:
            while True:
                for status in list(self._statuses):
                    if status.get("event") == event:
                        self._statuses.remove(status)
                        if event != "error":
                            return status
                    if status.get("event") == "error":
                        self._statuses.remove(status)
                        raise TFTError(status.get("msg", "firmware error"))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TFTError(f"Timed out waiting for status event {event!r}.")
                self._status_cv.wait(remaining)

    def set_channels(self, channels: Iterable[str], *, retain: bool = True) -> None:
        names = [_check_topic(name, "channel") for name in channels]
        if not names:
            raise ValueError("'channels' must contain at least one topic.")
        if len(names) > 8:
            raise ValueError("Firmware supports at most 8 channels.")
        self._publish_json(self._control_topic, names, retain=retain)

    def configure_single_channel(self, topic: Optional[str] = None, *, retain: bool = True) -> None:
        if topic is not None:
            self.command_topic = topic
        self.set_channels([self._command_topic], retain=retain)

    @contextmanager
    def batch(self) -> Generator["MQTTTFTTerminal", None, None]:
        commands: List[dict] = []
        self._batch_stack.append(commands)
        try:
            yield self
        except Exception:
            raise
        else:
            if commands:
                self._publish_json(self._command_topic, commands)
        finally:
            self._batch_stack.pop()

    def send_many(self, commands: Iterable[dict]) -> None:
        payload = list(commands)
        if not payload:
            raise ValueError("'commands' must not be empty.")
        for idx, command in enumerate(payload):
            if not isinstance(command, dict):
                raise TypeError(f"Command at index {idx} is not a dict.")
        self._publish_json(self._command_topic, payload)

    def send_raw(self, payload: Union[dict, List[dict]], *, topic: Optional[str] = None) -> None:
        if not isinstance(payload, (dict, list)):
            raise TypeError("'payload' must be a dict command or list of dict commands.")
        self._publish_json(topic or self._command_topic, payload)

    def sync(self) -> dict:
        return self.query()

    def clear(self) -> None:
        self._send({"cmd": "clear"})

    def set_background(self, color: ColorSpec) -> None:
        self._send({"cmd": "bg", "color": _normalise_color(color)})

    def text(self, x: int, y: int, text: str, *, color: ColorSpec = "white", size: int = 1) -> None:
        self._send({
            "cmd": "text",
            "x": _check_coord(x, "x", self._width),
            "y": _check_coord(y, "y", self._height),
            "text": _check_text(text),
            "size": _check_size(size),
            "color": _normalise_color(color),
        })

    def _build_rect(self, cmd: str, x: int, y: int, w: int, h: int, color: ColorSpec) -> dict:
        return {
            "cmd": cmd,
            "x": _check_coord(x, "x", self._width),
            "y": _check_coord(y, "y", self._height),
            "w": _check_dim(w, "w"),
            "h": _check_dim(h, "h"),
            "color": _normalise_color(color),
        }

    def fill_rect(self, x: int, y: int, w: int, h: int, color: ColorSpec) -> None:
        self._send(self._build_rect("fill_rect", x, y, w, h, color))

    def rect(self, x: int, y: int, w: int, h: int, color: ColorSpec) -> None:
        self._send(self._build_rect("rect", x, y, w, h, color))

    def _build_circle(self, cmd: str, x: int, y: int, r: int, color: ColorSpec) -> dict:
        return {
            "cmd": cmd,
            "x": _check_coord(x, "x", self._width),
            "y": _check_coord(y, "y", self._height),
            "r": _check_dim(r, "r"),
            "color": _normalise_color(color),
        }

    def fill_circle(self, x: int, y: int, r: int, color: ColorSpec) -> None:
        self._send(self._build_circle("fill_circle", x, y, r, color))

    def circle(self, x: int, y: int, r: int, color: ColorSpec) -> None:
        self._send(self._build_circle("circle", x, y, r, color))

    def _build_ellipse(self, cmd: str, x: int, y: int, rx: int, ry: int, color: ColorSpec) -> dict:
        return {
            "cmd": cmd,
            "x": _check_coord(x, "x", self._width),
            "y": _check_coord(y, "y", self._height),
            "rx": _check_dim(rx, "rx"),
            "ry": _check_dim(ry, "ry"),
            "color": _normalise_color(color),
        }

    def ellipse(self, x: int, y: int, rx: int, ry: int, color: ColorSpec) -> None:
        self._send(self._build_ellipse("ellipse", x, y, rx, ry, color))

    def fill_ellipse(self, x: int, y: int, rx: int, ry: int, color: ColorSpec) -> None:
        self._send(self._build_ellipse("fill_ellipse", x, y, rx, ry, color))

    def _build_arc(
        self,
        cmd: str,
        x: int,
        y: int,
        r1: int,
        r2: int,
        start: Union[int, float],
        end: Union[int, float],
        color: ColorSpec,
    ) -> dict:
        return {
            "cmd": cmd,
            "x": _check_coord(x, "x", self._width),
            "y": _check_coord(y, "y", self._height),
            "r1": _check_dim(r1, "r1"),
            "r2": _check_dim(r2, "r2"),
            "start": _check_number(start, "start"),
            "end": _check_number(end, "end"),
            "color": _normalise_color(color),
        }

    def arc(
        self,
        x: int,
        y: int,
        r1: int,
        r2: int,
        start: Union[int, float],
        end: Union[int, float],
        color: ColorSpec,
    ) -> None:
        self._send(self._build_arc("arc", x, y, r1, r2, start, end, color))

    def fill_arc(
        self,
        x: int,
        y: int,
        r1: int,
        r2: int,
        start: Union[int, float],
        end: Union[int, float],
        color: ColorSpec,
    ) -> None:
        self._send(self._build_arc("fill_arc", x, y, r1, r2, start, end, color))

    def hline(self, x: int, y: int, length: int, color: ColorSpec) -> None:
        self._send({
            "cmd": "hline",
            "x": _check_coord(x, "x", self._width),
            "y": _check_coord(y, "y", self._height),
            "len": _check_dim(length, "length"),
            "color": _normalise_color(color),
        })

    def vline(self, x: int, y: int, length: int, color: ColorSpec) -> None:
        self._send({
            "cmd": "vline",
            "x": _check_coord(x, "x", self._width),
            "y": _check_coord(y, "y", self._height),
            "len": _check_dim(length, "length"),
            "color": _normalise_color(color),
        })

    def line(self, x0: int, y0: int, x1: int, y1: int, color: ColorSpec) -> None:
        self._send({
            "cmd": "line",
            "x0": _check_coord(x0, "x0", self._width),
            "y0": _check_coord(y0, "y0", self._height),
            "x1": _check_coord(x1, "x1", self._width),
            "y1": _check_coord(y1, "y1", self._height),
            "color": _normalise_color(color),
        })

    def fill_screen(self, color: ColorSpec) -> None:
        self._send({"cmd": "fill_screen", "color": _normalise_color(color)})

    def pixel(self, x: int, y: int, color: ColorSpec) -> None:
        self._send({
            "cmd": "pixel",
            "x": _check_coord(x, "x", self._width),
            "y": _check_coord(y, "y", self._height),
            "color": _normalise_color(color),
        })

    def _build_triangle(
        self,
        cmd: str,
        x0: int, y0: int,
        x1: int, y1: int,
        x2: int, y2: int,
        color: ColorSpec,
    ) -> dict:
        return {
            "cmd": cmd,
            "x0": _check_coord(x0, "x0", self._width),
            "y0": _check_coord(y0, "y0", self._height),
            "x1": _check_coord(x1, "x1", self._width),
            "y1": _check_coord(y1, "y1", self._height),
            "x2": _check_coord(x2, "x2", self._width),
            "y2": _check_coord(y2, "y2", self._height),
            "color": _normalise_color(color),
        }

    def triangle(self, x0: int, y0: int, x1: int, y1: int, x2: int, y2: int, color: ColorSpec) -> None:
        self._send(self._build_triangle("triangle", x0, y0, x1, y1, x2, y2, color))

    def fill_triangle(self, x0: int, y0: int, x1: int, y1: int, x2: int, y2: int, color: ColorSpec) -> None:
        self._send(self._build_triangle("fill_triangle", x0, y0, x1, y1, x2, y2, color))

    def _build_rounded_rect(self, cmd: str, x: int, y: int, w: int, h: int, r: int, color: ColorSpec) -> dict:
        return {
            "cmd": cmd,
            "x": _check_coord(x, "x", self._width),
            "y": _check_coord(y, "y", self._height),
            "w": _check_dim(w, "w"),
            "h": _check_dim(h, "h"),
            "r": _check_dim(r, "r"),
            "color": _normalise_color(color),
        }

    def rounded_rect(self, x: int, y: int, w: int, h: int, r: int, color: ColorSpec) -> None:
        self._send(self._build_rounded_rect("rounded_rect", x, y, w, h, r, color))

    def fill_rounded_rect(self, x: int, y: int, w: int, h: int, r: int, color: ColorSpec) -> None:
        self._send(self._build_rounded_rect("fill_rounded_rect", x, y, w, h, r, color))

    def border(self, color: ColorSpec, thickness: int = 1) -> None:
        t = _check_dim(thickness, "thickness")
        c = _normalise_color(color)
        for i in range(t):
            self._send({
                "cmd": "rect",
                "x": i,
                "y": i,
                "w": self._width - 2 * i,
                "h": self._height - 2 * i,
                "color": c,
            })

    def set_rotation(self, r: int) -> None:
        rv = _check_int(r, "r")
        if rv not in (0, 1, 2, 3):
            raise ValueError("Parameter 'r' must be 0, 1, 2, or 3.")
        if self._wait_for_status:
            status = self._request_status({"cmd": "rotation", "r": rv}, "rotation")
            self._width = int(status["w"])
            self._height = int(status["h"])
        else:
            self._send({"cmd": "rotation", "r": rv})
            self._width, self._height = self._profile.dimensions_for_rotation(rv)

    @property
    def rotation_map(self) -> dict:
        return {
            0: "portrait",
            1: "landscape",
            2: "portrait-flip",
            3: "landscape-flip",
        }

    def ping(self) -> int:
        status = self._request_status({"cmd": "ping"}, "pong")
        return int(status["uptime_ms"])

    def query(self) -> dict:
        status = self._request_status({"cmd": "query"}, "query")
        result = {
            "w": int(status["w"]),
            "h": int(status["h"]),
            "rotation": int(status["rotation"]),
            "bg": int(status["bg"]),
            "free_heap": int(status["free_heap"]),
        }
        self._width = result["w"]
        self._height = result["h"]
        return result

    def set_brightness(self, value: int) -> None:
        v = _check_int(value, "value")
        if not 0 <= v <= 255:
            raise ValueError("Parameter 'value' must be in range [0, 255].")
        self._send({"cmd": "brightness", "v": v})


# Convenience alias for code that imports the TCP helper class name.
TFTTerminal = MQTTTFTTerminal
