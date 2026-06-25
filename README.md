# ESP8266 MQTT TFT Terminal

An ESP8266 firmware that turns an SPI TFT display into a remotely controlled terminal driven by MQTT commands. Publish JSON drawing commands to an MQTT topic; the device renders them on screen in real time.

---

## How It Works

The firmware connects the ESP8266 to a WiFi network and an MQTT broker. A controlling application (Node-RED, Python script, Home Assistant, etc.) sends JSON-formatted drawing commands to named MQTT topics. The device subscribes to those topics and executes each command — drawing text, shapes, and controlling the screen — updating the display immediately.

Multiple independent content feeds ("channels") can be configured. A hardware button cycles through them on the device.

```
[MQTT Publisher] --topic: weather--> [Broker] --subscribe--> [ESP8266] --> [TFT Screen]
[MQTT Publisher] --topic: stocks --> [Broker]
[MQTT Publisher] --topic: sysmon --> [Broker]

tft/control  <-- sets the active channel list
tft/status   <-- device publishes boot events, errors, query responses
```

---

## Hardware

| Component | Details |
|-----------|---------|
| MCU | ESP8266 (ESP-12E, NodeMCU, D1 Mini) |
| Display | SPI TFT using Arduino_GFX (see supported list below) |
| Button | Momentary push-button on GPIO4 (D2) — cycles channels |
| Backlight | Optional PWM pin for brightness control |

### SPI TFT Wiring (default)

| Signal | GPIO | NodeMCU Pin |
|--------|------|-------------|
| CS     | 15   | D8          |
| DC     | 2    | D4          |
| RST    | 0    | D3          |
| MOSI   | 13   | D7          |
| SCLK   | 14   | D5          |

### Supported Displays

Enable exactly **one** `#define` in the user configuration section:

| Define | Display | Interface | Resolution |
|--------|---------|-----------|------------|
| `DISPLAY_ILI9341` | ILI9341 | SPI | 240×320 |
| `DISPLAY_ST7796` | ST7796 | SPI | 320×480 |
| `DISPLAY_ST7735` | ST7735 | SPI | 128×160 or 128×128 with geometry config |

---

## Configuration

Edit the user configuration block near the top of `src/main.ino`:

```cpp
// WiFi
const char* WIFI_SSID     = "YOUR_SSID";
const char* WIFI_PASSWORD = "YOUR_PASSWORD";
const char* WIFI_HOSTNAME = "tft-mqtt";

// MQTT Broker
const char* MQTT_HOST = "192.168.1.10";
const int   MQTT_PORT = 1883;
const char* MQTT_USER = "";   // leave blank if no auth
const char* MQTT_PASS = "";

// Topics
const char* MQTT_CTRL_TOPIC   = "tft/control";
const char* MQTT_STATUS_TOPIC = "tft/status";

// Display — uncomment ONE:
#define DISPLAY_ILI9341

// SPI pins
const int TFT_CS  = 15;
const int TFT_DC  = 2;
const int TFT_RST = 0;

// Rotation: 0=portrait, 1=landscape, 2=portrait-flip, 3=landscape-flip
const int TFT_ROTATION = 1;

// Backlight PWM pin (-1 to disable)
const int TFT_BL_PIN = -1;
```

---

## MQTT Protocol

### Setting the Channel List

Publish a JSON array of topic names to the control topic. The device subscribes to each topic as a display channel.

**Topic:** `tft/control`
```json
["weather", "stockticker", "sysmon", "alerts"]
```

Up to 8 channel names, each up to 64 characters. The device re-evaluates its active channel when the list changes, staying on the current channel if it is still present.

### Sending Drawing Commands

Publish to a named channel topic. Commands may be a single JSON object or an array of objects.

**Single command:**
```json
{"cmd": "text", "x": 10, "y": 20, "text": "Hello", "color": "#0F0", "size": 2}
```

**Command array (batched):**
```json
[
  {"cmd": "fill_screen", "color": "#001830"},
  {"cmd": "text", "x": 50, "y": 100, "text": "Weather", "color": "#FFFFFF"},
  {"cmd": "circle", "x": 160, "y": 120, "r": 30, "color": "#00FF00"}
]
```

### Device Status Messages

The device publishes JSON objects to the status topic in response to events and queries:

| Event | Example Payload |
|-------|----------------|
| Boot | `{"event":"boot","ip":"192.168.1.42","hostname":"tft-mqtt"}` |
| Ping response | `{"event":"pong","uptime_ms":12345}` |
| Query response | `{"event":"query","w":320,"h":240,"rotation":1,"bg":0,"free_heap":28000}` |
| Error | `{"event":"error","msg":"color validation failed"}` |

---

## Drawing Commands Reference

### Colors

Colors are accepted as:
- **Hex string:** `"#RGB"` (12-bit shorthand) or `"#RRGGBB"` (24-bit) — converted internally to RGB565
- **Integer:** a raw uint16 RGB565 value

### Text

```json
{"cmd": "text", "x": 0, "y": 0, "text": "Hello!", "color": "#FFF", "size": 2}
```

| Field | Type | Description |
|-------|------|-------------|
| `x`, `y` | int | Top-left pixel position |
| `text` | string | Text to render |
| `color` | color | Text color |
| `size` | int | Font scale multiplier (1–8) |

### Pixel

```json
{"cmd": "pixel", "x": 10, "y": 10, "color": "#F00"}
```

### Lines

```json
{"cmd": "line",  "x0": 0, "y0": 0, "x1": 100, "y1": 100, "color": "#FFF"}
{"cmd": "hline", "x": 0, "y": 50, "len": 200, "color": "#0FF"}
{"cmd": "vline", "x": 50, "y": 0, "len": 100, "color": "#F0F"}
```

### Rectangles

```json
{"cmd": "rect",       "x": 10, "y": 10, "w": 80, "h": 40, "color": "#0F0"}
{"cmd": "fill_rect",  "x": 10, "y": 10, "w": 80, "h": 40, "color": "#0F0"}
{"cmd": "rounded_rect",      "x": 10, "y": 10, "w": 80, "h": 40, "r": 8, "color": "#0F0"}
{"cmd": "fill_rounded_rect", "x": 10, "y": 10, "w": 80, "h": 40, "r": 8, "color": "#0F0"}
```

### Circles

```json
{"cmd": "circle",      "x": 100, "y": 100, "r": 40, "color": "#FF0"}
{"cmd": "fill_circle", "x": 100, "y": 100, "r": 40, "color": "#FF0"}
```

### Ellipses And Arcs

```json
{"cmd": "ellipse",      "x": 120, "y": 160, "rx": 50, "ry": 30, "color": "#0FF"}
{"cmd": "fill_ellipse", "x": 120, "y": 160, "rx": 50, "ry": 30, "color": "#0FF"}
{"cmd": "arc",          "x": 120, "y": 160, "r1": 50, "r2": 44, "start": 30, "end": 300, "color": "#FF0"}
{"cmd": "fill_arc",     "x": 120, "y": 160, "r1": 50, "r2": 25, "start": 30, "end": 300, "color": "#FF0"}
```

### Triangles

```json
{"cmd": "triangle",      "x0":10,"y0":80, "x1":60,"y1":0, "x2":110,"y2":80, "color":"#F00"}
{"cmd": "fill_triangle", "x0":10,"y0":80, "x1":60,"y1":0, "x2":110,"y2":80, "color":"#F00"}
```

### Display Control

| Command | Fields | Description |
|---------|--------|-------------|
| `clear` | — | Fill screen with stored background color |
| `fill_screen` | `color` | Fill screen with a color |
| `bg` | `color` | Set background color without redrawing |
| `rotation` | `r` (0–3) | Change display orientation |
| `brightness` | `v` (0–255) | Set backlight PWM level (requires `TFT_BL_PIN`) |

### Queries

| Command | Description | Response event |
|---------|-------------|---------------|
| `ping` | Request uptime | `pong` |
| `query` | Request display state and free heap | `query` |

## Channels and the Hardware Button

The device supports up to **8 display channels**. Each channel maps to one MQTT topic. The button on GPIO4 cycles through them in order:

1. Press button → current channel unsubscribed, next channel subscribed
2. A 2-second splash screen shows the new channel name
3. The device resumes rendering commands from the new topic

When the control topic delivers a new channel list, the device:
- Unsubscribes all current channels
- Subscribes to the new list
- Stays on the same channel index if it still exists, otherwise resets to channel 0

---

## Connection Lifecycle

```
Power on
  └─> WiFi connect (status shown on display)
        └─> MQTT connect
              └─> Subscribe to tft/control
                    └─> Publish boot event to tft/status
                          └─> Wait for control message → subscribe channels
                                └─> Render incoming commands
```

The device reconnects automatically on WiFi or MQTT loss. All errors are logged to Serial and published to `tft/status`.

---

## Build & Flash

**Requirements:** PlatformIO (VS Code extension or CLI)

```bash
# Build
pio run

# Upload
pio run --target upload

# Serial monitor
pio device monitor
```

**platformio.ini dependencies installed automatically:**
- `knolleary/PubSubClient` — MQTT client
- `bblanchon/ArduinoJson` — JSON parsing
- `moononournation/GFX Library for Arduino` — graphics abstraction and display drivers

---

## Limits

| Parameter | Value |
|-----------|-------|
| Max channels | 8 |
| Max topic name length | 64 characters |
| Max MQTT payload | 2048 bytes |
| Color format | RGB565 (16-bit) |
| Coordinate range | 16-bit signed, clipped to display bounds |
| Text size range | 1–8 |
| Brightness range | 0–255 |
