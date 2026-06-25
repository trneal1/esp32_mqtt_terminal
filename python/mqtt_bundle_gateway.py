"""TCP-to-MQTT bundling gateway.

Each configured flow starts one TCP receiver. In bundle mode, incoming bytes are
collected until there has been no received data for the flow's gap time, then
the collected bytes are published as one MQTT message to that flow's MQTT topic.
Bundles are published as JSON arrays when they are not already in array form. In
command mode, each complete JSON command is published as its own MQTT message.

This app uses only the Python standard library and publishes MQTT 3.1.1 QoS 0.
"""

from __future__ import annotations

import argparse
import asyncio
import html
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import struct
import threading
from urllib.parse import parse_qs, quote, unquote, urlparse
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = Path(__file__).with_name("mqtt_bundle_gateway_flows.json")
BUNDLE_MODE = "bundle"
COMMAND_MODE = "command"
DELIVERY_MODES = (BUNDLE_MODE, COMMAND_MODE)


@dataclass
class Flow:
    name: str
    listen_host: str
    listen_port: int
    mqtt_broker: str
    mqtt_port: int
    mqtt_topic: str
    gap_seconds: float
    mode: str = BUNDLE_MODE
    retain: bool = True
    username: str = ""
    password: str = ""


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
    flags = 0x02  # clean session
    if username:
        flags |= 0x80
    if password:
        flags |= 0x40
    body = (
        mqtt_string("MQTT")
        + bytes([4, flags])
        + struct.pack("!H", 30)
        + mqtt_string(client_id)
    )
    if username:
        body += mqtt_string(username)
    if password:
        body += mqtt_string(password)
    return bytes([0x10]) + encode_remaining_length(len(body)) + body


def mqtt_publish_packet(topic: str, payload: bytes, *, retain: bool) -> bytes:
    body = mqtt_string(topic) + payload
    fixed_header = 0x30 | (0x01 if retain else 0)
    return bytes([fixed_header]) + encode_remaining_length(len(body)) + body


def read_connack(sock: socket.socket) -> None:
    response = sock.recv(4)
    if len(response) != 4 or response[0] != 0x20 or response[1] != 0x02:
        raise RuntimeError(f"Invalid MQTT CONNACK: {response!r}")
    if response[3] != 0:
        raise RuntimeError(f"MQTT broker rejected connection, code={response[3]}")


def publish_mqtt(flow: Flow, payload: bytes) -> None:
    client_id = f"bundle-gateway-{flow.name}-{uuid.uuid4().hex[:8]}"
    with socket.create_connection((flow.mqtt_broker, flow.mqtt_port), timeout=10) as sock:
        sock.settimeout(10)
        sock.sendall(mqtt_connect_packet(client_id, flow.username, flow.password))
        read_connack(sock)
        sock.sendall(mqtt_publish_packet(flow.mqtt_topic, payload, retain=flow.retain))
        sock.sendall(bytes([0xE0, 0x00]))  # DISCONNECT


def format_bundle_payload(payload: bytes) -> bytes:
    stripped = payload.strip()
    if not stripped or (stripped.startswith(b"[") and stripped.endswith(b"]")):
        return stripped

    try:
        text = stripped.decode("utf-8")
        decoder = json.JSONDecoder()
        index = 0
        values: list[Any] = []
        while index < len(text):
            while index < len(text) and text[index].isspace():
                index += 1
            if index < len(text) and text[index] == ",":
                index += 1
                continue
            if index >= len(text):
                break
            value, index = decoder.raw_decode(text, index)
            values.append(value)
        if values:
            return json.dumps(values, separators=(",", ":")).encode("utf-8")
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass

    return b"[" + stripped + b"]"


def encode_command_payload(command: Any) -> bytes | None:
    if not isinstance(command, dict):
        print(f"[command parser] skipped non-object JSON value: {type(command).__name__}")
        return None
    return json.dumps(command, separators=(",", ":")).encode("utf-8")


def extract_command_payloads(buffer: bytearray, *, final: bool = False) -> list[bytes]:
    try:
        text = buffer.decode("utf-8")
    except UnicodeDecodeError:
        if final:
            print("[command parser] buffered data is not valid UTF-8; discarded")
            buffer.clear()
        return []

    decoder = json.JSONDecoder()
    index = 0
    payloads: list[bytes] = []
    consumed = 0

    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index < len(text) and text[index] == ",":
            index += 1
            continue
        if index >= len(text):
            consumed = len(text)
            break

        try:
            value, index = decoder.raw_decode(text, index)
        except json.JSONDecodeError as exc:
            if final:
                raise ValueError(f"JSON parse error at byte {exc.pos}: {exc.msg}") from exc
            consumed = index
            break

        consumed = index
        values = value if isinstance(value, list) else [value]
        for command in values:
            payload = encode_command_payload(command)
            if payload is not None:
                payloads.append(payload)

    if consumed:
        del buffer[: len(text[:consumed].encode("utf-8"))]
    return payloads


def load_flows(config_path: Path) -> list[Flow]:
    if not config_path.exists():
        return []
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{config_path} must contain a JSON array.")
    flows = [Flow(**item) for item in raw]
    for flow in flows:
        validate_flow(flow)
    return flows


def save_flows(config_path: Path, flows: list[Flow]) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    data = [asdict(flow) for flow in sorted(flows, key=lambda item: item.name)]
    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def find_flow(flows: list[Flow], name: str) -> Flow | None:
    return next((flow for flow in flows if flow.name == name), None)


def positive_port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be in range 1..65535")
    return port


def positive_gap(value: str) -> float:
    gap = float(value)
    if gap <= 0:
        raise argparse.ArgumentTypeError("gap must be greater than 0")
    return gap


def validate_flow(flow: Flow) -> None:
    if not flow.name.strip():
        raise ValueError("Flow name is required.")
    if "/" in flow.name or "\\" in flow.name:
        raise ValueError("Flow name may not contain slashes.")
    if not 1 <= flow.listen_port <= 65535:
        raise ValueError("TCP listen port must be in range 1..65535.")
    if not flow.listen_host.strip():
        raise ValueError("TCP listen host is required.")
    if not flow.mqtt_broker.strip():
        raise ValueError("MQTT broker is required.")
    if not 1 <= flow.mqtt_port <= 65535:
        raise ValueError("MQTT port must be in range 1..65535.")
    if not flow.mqtt_topic.strip():
        raise ValueError("MQTT topic is required.")
    if flow.mode not in DELIVERY_MODES:
        raise ValueError(f"Mode must be one of: {', '.join(DELIVERY_MODES)}.")
    if flow.mode == BUNDLE_MODE and flow.gap_seconds <= 0:
        raise ValueError("Gap seconds must be greater than 0.")


def flow_from_form(form: dict[str, list[str]], *, existing_name: str | None = None) -> Flow:
    def get_text(name: str, default: str = "") -> str:
        return form.get(name, [default])[0].strip()

    def get_int(name: str, default: int) -> int:
        raw = get_text(name, str(default))
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"{name.replace('_', ' ').title()} must be an integer.") from exc

    def get_float(name: str, default: float) -> float:
        raw = get_text(name, str(default))
        try:
            return float(raw)
        except ValueError as exc:
            raise ValueError(f"{name.replace('_', ' ').title()} must be a number.") from exc

    mode = get_text("mode", BUNDLE_MODE)
    gap_seconds = get_float("gap_seconds", 1.0) if get_text("gap_seconds") else 1.0

    flow = Flow(
        name=existing_name or get_text("name"),
        listen_host=get_text("listen_host", "0.0.0.0"),
        listen_port=get_int("listen_port", 0),
        mqtt_broker=get_text("mqtt_broker"),
        mqtt_port=get_int("mqtt_port", 1883),
        mqtt_topic=get_text("mqtt_topic"),
        gap_seconds=gap_seconds,
        mode=mode,
        retain="retain" in form,
        username=get_text("username"),
        password=get_text("password"),
    )
    validate_flow(flow)
    return flow


def h(value: Any) -> str:
    return html.escape(str(value), quote=True)


def add_flow(args: argparse.Namespace) -> None:
    config_path = Path(args.config)
    flows = load_flows(config_path)
    if find_flow(flows, args.name):
        raise SystemExit(f"Flow {args.name!r} already exists.")
    flow = Flow(
        name=args.name,
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        mqtt_broker=args.mqtt_broker,
        mqtt_port=args.mqtt_port,
        mqtt_topic=args.mqtt_topic,
        gap_seconds=args.gap_seconds,
        mode=args.mode,
        retain=not args.no_retain,
        username=args.username,
        password=args.password,
    )
    validate_flow(flow)
    flows.append(flow)
    save_flows(config_path, flows)
    print(f"Added flow {args.name!r} to {config_path}")


def edit_flow(args: argparse.Namespace) -> None:
    config_path = Path(args.config)
    flows = load_flows(config_path)
    flow = find_flow(flows, args.name)
    if flow is None:
        raise SystemExit(f"Flow {args.name!r} does not exist.")

    updates: dict[str, Any] = {
        "listen_host": args.listen_host,
        "listen_port": args.listen_port,
        "mqtt_broker": args.mqtt_broker,
        "mqtt_port": args.mqtt_port,
        "mqtt_topic": args.mqtt_topic,
        "gap_seconds": args.gap_seconds,
        "mode": args.mode,
        "username": args.username,
        "password": args.password,
    }
    for field, value in updates.items():
        if value is not None:
            setattr(flow, field, value)
    if args.retain:
        flow.retain = True
    if args.no_retain:
        flow.retain = False

    validate_flow(flow)
    save_flows(config_path, flows)
    print(f"Edited flow {args.name!r} in {config_path}")


def delete_flow(args: argparse.Namespace) -> None:
    config_path = Path(args.config)
    flows = load_flows(config_path)
    kept = [flow for flow in flows if flow.name != args.name]
    if len(kept) == len(flows):
        raise SystemExit(f"Flow {args.name!r} does not exist.")
    save_flows(config_path, kept)
    print(f"Deleted flow {args.name!r} from {config_path}")


def list_flows(args: argparse.Namespace) -> None:
    flows = load_flows(Path(args.config))
    if not flows:
        print("No flows configured.")
        return
    for flow in sorted(flows, key=lambda item: item.name):
        retain = "retain" if flow.retain else "no-retain"
        gap = f"gap={flow.gap_seconds:g}s" if flow.mode == BUNDLE_MODE else "gap=n/a"
        print(
            f"{flow.name}: tcp://{flow.listen_host}:{flow.listen_port} -> "
            f"mqtt://{flow.mqtt_broker}:{flow.mqtt_port}/{flow.mqtt_topic} "
            f"mode={flow.mode} {gap} {retain}"
        )


async def publish_bundle(flow: Flow, payload: bytes) -> None:
    formatted = format_bundle_payload(payload)
    await asyncio.to_thread(publish_mqtt, flow, formatted)
    print(
        f"[{flow.name}] published {len(formatted)} byte(s) to "
        f"{flow.mqtt_topic} retain={flow.retain}"
    )


async def publish_command(flow: Flow, payload: bytes) -> None:
    await asyncio.to_thread(publish_mqtt, flow, payload)
    print(
        f"[{flow.name}] published command {len(payload)} byte(s) to "
        f"{flow.mqtt_topic} retain={flow.retain}"
    )


async def handle_bundle_client(reader: asyncio.StreamReader, flow: Flow, buffer: bytearray) -> None:
    while True:
        try:
            data = await asyncio.wait_for(reader.read(4096), timeout=flow.gap_seconds)
        except asyncio.TimeoutError:
            if buffer:
                payload = bytes(buffer)
                buffer.clear()
                await publish_bundle(flow, payload)
            continue

        if not data:
            if buffer:
                await publish_bundle(flow, bytes(buffer))
                buffer.clear()
            break

        buffer.extend(data)


async def handle_command_client(reader: asyncio.StreamReader, flow: Flow, buffer: bytearray) -> None:
    while True:
        data = await reader.read(4096)
        if not data:
            for payload in extract_command_payloads(buffer, final=True):
                await publish_command(flow, payload)
            break

        buffer.extend(data)
        for payload in extract_command_payloads(buffer):
            await publish_command(flow, payload)


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, flow: Flow) -> None:
    peer = writer.get_extra_info("peername")
    print(f"[{flow.name}] client connected: {peer}")
    buffer = bytearray()
    try:
        if flow.mode == COMMAND_MODE:
            await handle_command_client(reader, flow, buffer)
        else:
            await handle_bundle_client(reader, flow, buffer)
    except Exception as exc:
        print(f"[{flow.name}] client error from {peer}: {exc}")
    finally:
        writer.close()
        await writer.wait_closed()
        print(f"[{flow.name}] client disconnected: {peer}")


async def run_gateway(args: argparse.Namespace) -> None:
    flows = load_flows(Path(args.config))
    if not flows:
        raise SystemExit("No flows configured. Use the add command first.")

    servers: list[asyncio.Server] = []
    for flow in sorted(flows, key=lambda item: item.name):
        server = await asyncio.start_server(
            lambda reader, writer, current_flow=flow: handle_client(reader, writer, current_flow),
            flow.listen_host,
            flow.listen_port,
        )
        servers.append(server)
        addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
        print(
            f"[{flow.name}] listening on {addresses}; "
            f"publishing to {flow.mqtt_broker}:{flow.mqtt_port}/{flow.mqtt_topic} "
            f"mode={flow.mode}"
        )

    try:
        await asyncio.gather(*(server.serve_forever() for server in servers))
    finally:
        for server in servers:
            server.close()
            await server.wait_closed()


class GatewayRuntime:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, name="mqtt-bundle-gateway", daemon=True)
        self.servers: dict[str, tuple[Flow, asyncio.Server]] = {}

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self) -> None:
        self.thread.start()

    def sync(self, flows: list[Flow]) -> None:
        future = asyncio.run_coroutine_threadsafe(self._sync(flows), self.loop)
        future.result(timeout=10)

    def stop(self) -> None:
        future = asyncio.run_coroutine_threadsafe(self._stop_all(), self.loop)
        future.result(timeout=10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)
        self.loop.close()

    async def _sync(self, flows: list[Flow]) -> None:
        desired = {flow.name: flow for flow in flows}
        for name, (flow, server) in list(self.servers.items()):
            if desired.get(name) != flow:
                await self._close_server(name, server)

        for flow in sorted(flows, key=lambda item: item.name):
            if flow.name in self.servers:
                continue
            server = await asyncio.start_server(
                lambda reader, writer, current_flow=flow: handle_client(reader, writer, current_flow),
                flow.listen_host,
                flow.listen_port,
            )
            self.servers[flow.name] = (flow, server)
            addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
            print(
                f"[{flow.name}] listening on {addresses}; "
                f"publishing to {flow.mqtt_broker}:{flow.mqtt_port}/{flow.mqtt_topic} "
                f"mode={flow.mode}"
            )

    async def _close_server(self, name: str, server: asyncio.Server) -> None:
        server.close()
        await server.wait_closed()
        self.servers.pop(name, None)
        print(f"[{name}] TCP listener stopped")

    async def _stop_all(self) -> None:
        for name, (_, server) in list(self.servers.items()):
            await self._close_server(name, server)


def render_page(title: str, body: str, *, notice: str = "", error: str = "") -> bytes:
    notice_html = f'<div class="notice">{h(notice)}</div>' if notice else ""
    error_html = f'<div class="error">{h(error)}</div>' if error else ""
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{h(title)} - MQTT Bundle Gateway</title>
  <style>
    :root {{ color-scheme: light; font-family: Arial, sans-serif; }}
    body {{ margin: 0; background: #f6f8fb; color: #16202a; }}
    header {{ background: #12324a; color: white; padding: 18px 24px; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 24px; }}
    h1 {{ margin: 0; font-size: 24px; }}
    h2 {{ margin: 0 0 16px; font-size: 20px; }}
    a {{ color: #0b6694; }}
    .actions {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 0 0 18px; }}
    .button, button {{ border: 0; background: #0b6694; color: white; padding: 9px 12px; border-radius: 6px; text-decoration: none; cursor: pointer; font-size: 14px; }}
    .button.secondary, button.secondary {{ background: #52616f; }}
    .button.danger, button.danger {{ background: #a33636; }}
    table {{ width: 100%; border-collapse: collapse; background: white; box-shadow: 0 1px 4px rgba(20, 35, 50, 0.12); }}
    th, td {{ text-align: left; padding: 10px; border-bottom: 1px solid #dce3ea; vertical-align: top; }}
    th {{ background: #e9eef4; font-size: 13px; }}
    form.panel {{ background: white; padding: 18px; box-shadow: 0 1px 4px rgba(20, 35, 50, 0.12); }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; }}
    label {{ display: block; font-size: 13px; font-weight: 700; margin-bottom: 5px; }}
    input {{ box-sizing: border-box; width: 100%; padding: 9px; border: 1px solid #b7c3cf; border-radius: 5px; font-size: 14px; }}
    .checkbox {{ display: flex; align-items: center; gap: 8px; margin-top: 20px; }}
    .checkbox input {{ width: auto; }}
    .notice, .error {{ padding: 10px 12px; margin: 0 0 14px; border-radius: 6px; }}
    .notice {{ background: #e3f4ea; color: #184f2a; }}
    .error {{ background: #fde7e7; color: #7d1f1f; }}
    .empty {{ background: white; padding: 18px; box-shadow: 0 1px 4px rgba(20, 35, 50, 0.12); }}
    .inline {{ display: inline; }}
  </style>
</head>
<body>
  <header><h1>MQTT Bundle Gateway</h1></header>
  <main>
    {notice_html}
    {error_html}
    {body}
  </main>
</body>
</html>"""
    return page.encode("utf-8")


def render_flow_form(flow: Flow | None, *, action: str, submit_label: str) -> str:
    flow = flow or Flow("", "0.0.0.0", 0, "", 1883, "", 1.0)
    name_input = (
        f'<input name="name" value="{h(flow.name)}" required>'
        if not flow.name
        else f'<input value="{h(flow.name)}" disabled>'
    )
    retain_checked = " checked" if flow.retain else ""
    bundle_selected = " selected" if flow.mode == BUNDLE_MODE else ""
    command_selected = " selected" if flow.mode == COMMAND_MODE else ""
    return f"""
<h2>{h(submit_label)} Flow</h2>
<form class="panel" method="post" action="{h(action)}">
  <div class="grid">
    <div><label>Flow name</label>{name_input}</div>
    <div><label>TCP listen host</label><input name="listen_host" value="{h(flow.listen_host)}" required></div>
    <div><label>TCP listen port</label><input name="listen_port" type="number" min="1" max="65535" value="{h(flow.listen_port or '')}" required></div>
    <div><label>MQTT broker</label><input name="mqtt_broker" value="{h(flow.mqtt_broker)}" required></div>
    <div><label>MQTT port</label><input name="mqtt_port" type="number" min="1" max="65535" value="{h(flow.mqtt_port)}" required></div>
    <div><label>MQTT topic</label><input name="mqtt_topic" value="{h(flow.mqtt_topic)}" required></div>
    <div><label>Mode</label><select name="mode"><option value="{BUNDLE_MODE}"{bundle_selected}>Bundle JSON array</option><option value="{COMMAND_MODE}"{command_selected}>One MQTT message per command</option></select></div>
    <div><label>Bundle gap seconds</label><input name="gap_seconds" type="number" min="0.001" step="0.001" value="{h(flow.gap_seconds)}"></div>
    <div><label>MQTT username</label><input name="username" value="{h(flow.username)}"></div>
    <div><label>MQTT password</label><input name="password" type="password" value="{h(flow.password)}"></div>
    <div class="checkbox"><input id="retain" name="retain" type="checkbox"{retain_checked}><label for="retain">Retain MQTT messages</label></div>
  </div>
  <div class="actions" style="margin-top:18px">
    <button type="submit">{h(submit_label)}</button>
    <a class="button secondary" href="/">Cancel</a>
  </div>
</form>"""


def render_flow_list(flows: list[Flow]) -> str:
    if not flows:
        return """
<div class="actions"><a class="button" href="/add">Add flow</a></div>
<div class="empty">No flows configured.</div>"""
    rows = []
    for flow in sorted(flows, key=lambda item: item.name):
        encoded_name = quote(flow.name, safe="")
        retain = "yes" if flow.retain else "no"
        gap = f"{h(flow.gap_seconds)}s" if flow.mode == BUNDLE_MODE else "n/a"
        rows.append(
            f"""<tr>
  <td>{h(flow.name)}</td>
  <td>{h(flow.listen_host)}:{h(flow.listen_port)}</td>
  <td>{h(flow.mqtt_broker)}:{h(flow.mqtt_port)} / {h(flow.mqtt_topic)}</td>
  <td>{h(flow.mode)}</td>
  <td>{gap}</td>
  <td>{retain}</td>
  <td>
    <a class="button secondary" href="/edit/{encoded_name}">Edit</a>
    <form class="inline" method="post" action="/delete/{encoded_name}">
      <button class="danger" type="submit">Delete</button>
    </form>
  </td>
</tr>"""
        )
    return f"""
<div class="actions"><a class="button" href="/add">Add flow</a></div>
<table>
  <thead>
    <tr><th>Name</th><th>TCP receiver</th><th>MQTT destination</th><th>Mode</th><th>Gap</th><th>Retain</th><th>Actions</th></tr>
  </thead>
  <tbody>
    {''.join(rows)}
  </tbody>
</table>"""


class GatewayConfigHandler(BaseHTTPRequestHandler):
    config_path: Path
    runtime: GatewayRuntime | None = None

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[http] {self.address_string()} - {fmt % args}")

    def send_html(self, status: HTTPStatus, body: str, *, notice: str = "", error: str = "") -> None:
        data = render_page("Configuration", body, notice=notice, error=error)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def read_form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        return parse_qs(raw, keep_blank_values=True)

    def save_and_sync(self, flows: list[Flow]) -> None:
        if self.runtime is not None:
            self.runtime.sync(flows)
        save_flows(self.config_path, flows)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        flows = load_flows(self.config_path)
        if parsed.path == "/":
            notice = parse_qs(parsed.query).get("notice", [""])[0]
            self.send_html(HTTPStatus.OK, render_flow_list(flows), notice=notice)
            return
        if parsed.path == "/add":
            self.send_html(HTTPStatus.OK, render_flow_form(None, action="/add", submit_label="Add"))
            return
        if parsed.path.startswith("/edit/"):
            name = unquote(parsed.path.removeprefix("/edit/"))
            flow = find_flow(flows, name)
            if flow is None:
                self.send_html(HTTPStatus.NOT_FOUND, render_flow_list(flows), error=f"Flow {name!r} was not found.")
                return
            self.send_html(HTTPStatus.OK, render_flow_form(flow, action=f"/edit/{quote(name, safe='')}", submit_label="Save"))
            return
        self.send_html(HTTPStatus.NOT_FOUND, render_flow_list(flows), error="Page not found.")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        flows = load_flows(self.config_path)
        try:
            if parsed.path == "/add":
                flow = flow_from_form(self.read_form())
                if find_flow(flows, flow.name):
                    raise ValueError(f"Flow {flow.name!r} already exists.")
                flows.append(flow)
                self.save_and_sync(flows)
                self.redirect("/?notice=Flow%20added")
                return
            if parsed.path.startswith("/edit/"):
                name = unquote(parsed.path.removeprefix("/edit/"))
                flow = find_flow(flows, name)
                if flow is None:
                    raise ValueError(f"Flow {name!r} was not found.")
                updated = flow_from_form(self.read_form(), existing_name=name)
                flows = [updated if item.name == name else item for item in flows]
                self.save_and_sync(flows)
                self.redirect("/?notice=Flow%20saved")
                return
            if parsed.path.startswith("/delete/"):
                name = unquote(parsed.path.removeprefix("/delete/"))
                kept = [flow for flow in flows if flow.name != name]
                if len(kept) == len(flows):
                    raise ValueError(f"Flow {name!r} was not found.")
                self.save_and_sync(kept)
                self.redirect("/?notice=Flow%20deleted")
                return
            raise ValueError("Unsupported action.")
        except ValueError as exc:
            self.send_html(HTTPStatus.BAD_REQUEST, render_flow_list(flows), error=str(exc))


def run_http(args: argparse.Namespace) -> None:
    runtime = GatewayRuntime()
    handler = type(
        "ConfiguredGatewayConfigHandler",
        (GatewayConfigHandler,),
        {"config_path": Path(args.config), "runtime": runtime},
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    display_host = "localhost" if args.host in ("", "0.0.0.0", "::") else args.host
    print(f"HTTP configuration UI: http://{display_host}:{args.port}/")
    if args.host in ("0.0.0.0", "::"):
        print("Listening on all interfaces; use this machine's LAN IP from another device.")
    print(f"Config file: {Path(args.config)}")
    runtime.start()
    runtime.sync(load_flows(Path(args.config)))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping HTTP configuration UI")
    finally:
        server.server_close()
        runtime.stop()


def add_common_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help=f"Flow config JSON file. Default: {DEFAULT_CONFIG}",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TCP-to-MQTT bundling gateway.")
    add_common_config(parser)
    parser.add_argument("--host", "--http-host", default="0.0.0.0", help="HTTP host when no subcommand is given.")
    parser.add_argument("--port", "--http-port", type=positive_port, default=8080, help="HTTP port when no subcommand is given.")
    subparsers = parser.add_subparsers(dest="command")

    add_parser = subparsers.add_parser("add", help="Add a flow.")
    add_parser.add_argument("name")
    add_parser.add_argument("--listen-host", default="0.0.0.0")
    add_parser.add_argument("--listen-port", type=positive_port, required=True)
    add_parser.add_argument("--mqtt-broker", required=True)
    add_parser.add_argument("--mqtt-port", type=positive_port, default=1883)
    add_parser.add_argument("--mqtt-topic", required=True)
    add_parser.add_argument("--mode", choices=DELIVERY_MODES, default=BUNDLE_MODE)
    add_parser.add_argument("--gap-seconds", type=positive_gap, default=1.0)
    add_parser.add_argument("--no-retain", action="store_true")
    add_parser.add_argument("--username", default="")
    add_parser.add_argument("--password", default="")
    add_parser.set_defaults(func=add_flow)

    edit_parser = subparsers.add_parser("edit", help="Edit a flow.")
    edit_parser.add_argument("name")
    edit_parser.add_argument("--listen-host")
    edit_parser.add_argument("--listen-port", type=positive_port)
    edit_parser.add_argument("--mqtt-broker")
    edit_parser.add_argument("--mqtt-port", type=positive_port)
    edit_parser.add_argument("--mqtt-topic")
    edit_parser.add_argument("--mode", choices=DELIVERY_MODES)
    edit_parser.add_argument("--gap-seconds", type=positive_gap)
    edit_parser.add_argument("--retain", action="store_true")
    edit_parser.add_argument("--no-retain", action="store_true")
    edit_parser.add_argument("--username")
    edit_parser.add_argument("--password")
    edit_parser.set_defaults(func=edit_flow)

    delete_parser = subparsers.add_parser("delete", help="Delete a flow.")
    delete_parser.add_argument("name")
    delete_parser.set_defaults(func=delete_flow)

    list_parser = subparsers.add_parser("list", help="List flows.")
    list_parser.set_defaults(func=list_flows)

    run_parser = subparsers.add_parser("run", help="Run all configured flows.")
    run_parser.set_defaults(func=lambda args: asyncio.run(run_gateway(args)))

    web_parser = subparsers.add_parser("web", help="Run the HTTP configuration interface.")
    web_parser.add_argument("--host", default="0.0.0.0")
    web_parser.add_argument("--port", type=positive_port, default=8080)
    web_parser.set_defaults(func=run_http)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not hasattr(args, "func"):
        args.func = run_http
    args.func(args)


if __name__ == "__main__":
    main()
