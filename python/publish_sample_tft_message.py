"""Publish a retained sample TFT message to an MQTT display channel.

This script intentionally uses only the Python standard library. It sends one
MQTT 3.1.1 QoS 0 retained PUBLISH packet, which is enough for the TFT terminal.
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import uuid


def encode_remaining_length(value: int) -> bytes:
    encoded = bytearray()
    while True:
        byte = value % 128
        value //= 128
        if value:
            byte |= 0x80
        encoded.append(byte)
        if not value:
            return bytes(encoded)


def mqtt_string(value: str) -> bytes:
    data = value.encode("utf-8")
    if len(data) > 65535:
        raise ValueError("MQTT string is too long.")
    return struct.pack("!H", len(data)) + data


def mqtt_connect_packet(client_id: str, username: str = "", password: str = "") -> bytes:
    variable_header = (
        mqtt_string("MQTT")
        + bytes([4])  # MQTT 3.1.1
        + bytes([
            (0x80 if username else 0)
            | (0x40 if password else 0)
            | 0x02  # clean session
        ])
        + struct.pack("!H", 30)
    )
    payload = mqtt_string(client_id)
    if username:
        payload += mqtt_string(username)
    if password:
        payload += mqtt_string(password)
    body = variable_header + payload
    return bytes([0x10]) + encode_remaining_length(len(body)) + body


def mqtt_publish_packet(topic: str, payload: bytes, *, retain: bool = True) -> bytes:
    variable_header = mqtt_string(topic)
    body = variable_header + payload
    fixed_header = 0x30 | (0x01 if retain else 0)
    return bytes([fixed_header]) + encode_remaining_length(len(body)) + body


def read_connack(sock: socket.socket) -> None:
    response = sock.recv(4)
    if len(response) != 4 or response[0] != 0x20 or response[1] != 0x02:
        raise RuntimeError(f"Invalid MQTT CONNACK: {response!r}")
    if response[3] != 0:
        raise RuntimeError(f"MQTT broker rejected connection, code={response[3]}")


def publish_retained(
    broker: str,
    port: int,
    topic: str,
    payload: bytes,
    *,
    username: str = "",
    password: str = "",
    timeout: float = 5.0,
) -> None:
    client_id = f"sample-tft-{uuid.uuid4().hex[:8]}"
    with socket.create_connection((broker, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(mqtt_connect_packet(client_id, username, password))
        read_connack(sock)
        sock.sendall(mqtt_publish_packet(topic, payload, retain=True))
        sock.sendall(bytes([0xE0, 0x00]))  # DISCONNECT


def build_sample_message(channel: str) -> list[dict]:
    return [
        {"cmd": "fill_screen", "color": "#001018"},
        {"cmd": "fill_rect", "x": 0, "y": 0, "w": 320, "h": 36, "color": "#004C7A"},
        {"cmd": "text", "x": 8, "y": 10, "text": "MQTT TFT Sample", "color": "#FFFFFF", "size": 2},
        {"cmd": "text", "x": 12, "y": 58, "text": "Retained message", "color": "#00FFAA", "size": 2},
        {"cmd": "text", "x": 12, "y": 88, "text": f"Channel: {channel}", "color": "#FFFF00", "size": 1},
        {"cmd": "rect", "x": 8, "y": 48, "w": 304, "h": 72, "color": "#00BFFF"},
        {"cmd": "line", "x0": 8, "y0": 132, "x1": 312, "y1": 132, "color": "#666666"},
        {"cmd": "text", "x": 12, "y": 150, "text": "Publish drawing commands", "color": "#FFFFFF", "size": 1},
        {"cmd": "text", "x": 12, "y": 166, "text": "to update this screen.", "color": "#FFFFFF", "size": 1},
    ]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish a retained sample TFT command payload to an MQTT channel."
    )
    parser.add_argument("--broker", default="t2.lan", help="MQTT broker host or IP.")
    parser.add_argument("--port", type=int, default=1883, help="MQTT broker port.")
    parser.add_argument("--channel", default="tft/default", help="MQTT display channel topic.")
    parser.add_argument("--username", default="", help="MQTT username, if required.")
    parser.add_argument("--password", default="", help="MQTT password, if required.")
    args = parser.parse_args()

    payload = json.dumps(build_sample_message(args.channel), separators=(",", ":")).encode("utf-8")
    publish_retained(
        args.broker,
        args.port,
        args.channel,
        payload,
        username=args.username,
        password=args.password,
    )
    print(f"Published retained sample TFT message to {args.channel} on {args.broker}:{args.port}")


if __name__ == "__main__":
    main()
