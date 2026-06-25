/*
 * ESP32 MQTT TFT Terminal  (v3 — Arduino_GFX ILI9341 / ST7796 / ST7735 Edition)
 * ─────────────────────────────────────────────────────────────────────────────
 * Drop-in upgrade of v1.  The MQTT channel system (control topic, round-robin
 * switch, status events, single/array command dispatch) is 100% unchanged and
 * wire-compatible.  New in v3:
 *
 *   • Arduino_GFX display abstraction matching the TCP terminal v9 command set.
 *   • Full command parity with TCP terminal v9:
 *       ellipse | fill_ellipse | arc | fill_arc
 *       pixel | triangle | fill_triangle | rounded_rect | fill_rounded_rect
 *       brightness | ping | query
 *   • Backlight PWM pin support via TFT_BL_PIN.
 *   • "ping" and "query" publish their responses on MQTT_STATUS_TOPIC
 *     (MQTT is fire-and-forget; there is no TCP socket to reply on).
 *   • "rotation" likewise publishes new w/h on MQTT_STATUS_TOPIC.
 *
 * ─────────────────────────────────────────────────────────────────────────────
 *  SUPPORTED DISPLAYS  (uncomment ONE #define in USER CONFIGURATION)
 * ─────────────────────────────────────────────────────────────────────────────
 *  SPI TFT
 *   DISPLAY_ILI9341   Arduino_ILI9341     240×320   2.4 / 2.8 in
 *   DISPLAY_ST7796    Arduino_ST7796      320×480   4 in
 *   DISPLAY_ST7735    Arduino_ST7735      128×128 / 128×160   1.8 in
 *
 * ─────────────────────────────────────────────────────────────────────────────
 * DEPENDENCIES (Arduino Library Manager):
 *   WiFi           — bundled with the esp32 board package
 *   ArduinoJson    — v6.x by Benoit Blanchon
 *   PubSubClient   — v2.8+ by Nick O'Leary
 *   Arduino_GFX   — by moononournation
 *
 * ─────────────────────────────────────────────────────────────────────────────
 * MQTT PROTOCOL  (identical to v1 — fully wire-compatible)
 * ─────────────────────────────────────────────────────────────────────────────
 * Control channel  (MQTT_CTRL_TOPIC, retain recommended)
 *   Publish a JSON array of display channel names:
 *     ["weather","stockticker","sysmon","alerts"]
 *
 * Display channels  (one active at a time)
 *   Single command:  {"cmd":"text","x":10,"y":20,"text":"Hi","color":"#0F0"}
 *   Command array:   [{"cmd":"fill_screen","color":"#001830"},{"cmd":"text",...}]
 *
 * Status channel  (MQTT_STATUS_TOPIC, published by the terminal)
 *   {"event":"boot","ip":"192.168.1.42","hostname":"tft-mqtt"}
 *   {"event":"channel","name":"weather","index":0}
 *   {"event":"channels","count":4}
 *   {"event":"error","msg":"<description>"}
 *   {"event":"pong","uptime_ms":12345}               ← response to "ping"
 *   {"event":"query","w":320,"h":240,"rotation":1,   ← response to "query"
 *    "bg":0,"free_heap":28000}
 *   {"event":"rotation","r":1,"w":320,"h":240}        ← after rotation cmd
 *
 * Full command set (v3)
 * ─────────────────────
 *   text | clear | bg | fill_rect | rect | fill_circle | circle
 *   ellipse | fill_ellipse | arc | fill_arc
 *   hline | vline | line | fill_screen | rotation
 *   pixel | triangle | fill_triangle | rounded_rect | fill_rounded_rect
 *   brightness | ping | query
 *
 * ─────────────────────────────────────────────────────────────────────────────
 * SWITCH WIRING
 * ─────────────────────────────────────────────────────────────────────────────
 *   Switch input idles LOW and goes HIGH when pressed.
 *   Each press advances one channel.
 *
 * ─────────────────────────────────────────────────────────────────────────────
 */

// ═══════════════════════════════════════════════════════════════════════════
//  USER CONFIGURATION  ← edit only this section
// ═══════════════════════════════════════════════════════════════════════════

// WiFi
static const char* WIFI_SSID     = "TRNNET-2G";
static const char* WIFI_PASSWORD = "ripcord1";
static const char* WIFI_HOSTNAME = "esp-tft5";     // "" = SDK default

// MQTT broker
static const char* MQTT_HOST      = "t2.lan";
static const int   MQTT_PORT      = 1883;
static const char* MQTT_USER      = "";             // "" = no auth
static const char* MQTT_PASS      = "";
static const char* MQTT_CLIENT_ID = nullptr;        // nullptr = auto from chip ID

// MQTT topics
static const char* MQTT_CTRL_TOPIC   = "tft/control";
static const char* MQTT_STATUS_TOPIC = "tft/status";

// Channel list
static const uint8_t  MAX_CHANNELS    = 8;
static const uint8_t  MAX_TOPIC_LEN   = 64;
// Max MQTT payload bytes — must match mqtt.setBufferSize() call below.
static const uint16_t MAX_PAYLOAD_LEN = 17000;

// Channel splash duration (ms)
static const uint32_t SPLASH_MS   = 500;

// Switch pin (idle LOW, active HIGH). Use an external pull-down resistor.
static const uint8_t  SWITCH_GND_PIN = 19;
static const uint8_t  SWITCH_PIN     = 27;
static const uint8_t  SWITCH_VDD_PIN = 23;
static const uint32_t DEBOUNCE_MS = 50;

// ── Display selection ────────────────────────────────────────────────────
// Uncomment EXACTLY ONE:

 #define DISPLAY_ILI9341       // Arduino_ILI9341   240×320
//#define DISPLAY_ST7796           // Arduino_ST7796    320×480
// #define DISPLAY_ST7735        // Arduino_ST7735    128×128 / 128×160

// ── SPI bus selection ────────────────────────────────────────────────────
// Uncomment EXACTLY ONE:
// #define USE_VSPI          // VSPI  — default pins: MOSI=23, MISO=19, SCK=18
#define USE_HSPI             // HSPI  — default pins: MOSI=13, MISO=12, SCK=14

// ── SPI bus pin assignments ───────────────────────────────────────────────
#define VSPI_MOSI  23
#define VSPI_MISO  19
#define VSPI_SCK   18

#define HSPI_MOSI  13
#define HSPI_MISO  12
#define HSPI_SCK   14

// ── Display control pins (CS / DC / RST) ─────────────────────────────────
#define TFT_CS     15
#define TFT_DC     2
#define TFT_RST   -1  // use -1 to skip hardware reset

// ── ST7796 panel options ─────────────────────────────────────────────────
// Set ST7796_COLOR_ORDER_BGR to false if red/blue are swapped on your panel.
// Set ST7796_INVERT_DISPLAY to true if black and white are reversed.
#define ST7796_COLOR_ORDER_BGR true
#define ST7796_INVERT_DISPLAY  true

// ── ST7735 panel geometry ────────────────────────────────────────────────
// Common 1.8 in modules are 128x160 with zero offsets.  If your ST7735 panel
// needs color/row offsets, adjust these values for your tab variant.
#define ST7735_WIDTH        128
#define ST7735_HEIGHT       160
#define ST7735_COL_OFFSET1  0
#define ST7735_ROW_OFFSET1  0
#define ST7735_COL_OFFSET2  0
#define ST7735_ROW_OFFSET2  0
#define ST7735_BGR          true

// ── Screen rotation ──────────────────────────────────────────────────────
// 0=portrait  1=landscape  2=portrait-flip  3=landscape-flip
#define TFT_ROTATION  1

// ── Backlight PWM pin ────────────────────────────────────────────────────
// Set to a free GPIO to enable the "brightness" command.  -1 = disabled.
// Do NOT reuse any SPI pin above.
#define TFT_BL_PIN     21
#define TFT_BL_CHANNEL   0

// ═══════════════════════════════════════════════════════════════════════════
//  DISPLAY ABSTRACTION LAYER  — do not edit below this line
// ═══════════════════════════════════════════════════════════════════════════

#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <Arduino_GFX_Library.h>

// ── Exactly-one-display guard ────────────────────────────────────────────
#define DISP_COUNT_DEFINED ( \
    defined(DISPLAY_ILI9341) + \
    defined(DISPLAY_ST7796)  + \
    defined(DISPLAY_ST7735)    \
)
#if DISP_COUNT_DEFINED != 1
  #error "Uncomment EXACTLY ONE DISPLAY_xxx define in the USER CONFIGURATION section."
#endif

// ── SPI bus validation and instance ──────────────────────────────────────
#if defined(USE_VSPI) && defined(USE_HSPI)
  #error "Uncomment EXACTLY ONE of USE_VSPI or USE_HSPI."
#elif !defined(USE_VSPI) && !defined(USE_HSPI)
  #error "Uncomment exactly one of USE_VSPI or USE_HSPI in the USER CONFIGURATION section."
#elif defined(USE_VSPI)
  #define TFT_SPI_HOST VSPI
  #define TFT_MOSI     VSPI_MOSI
  #define TFT_MISO     VSPI_MISO
  #define TFT_SCK      VSPI_SCK
  #define SPI_BUS_NAME "VSPI"
#else
  #define TFT_SPI_HOST HSPI
  #define TFT_MOSI     HSPI_MOSI
  #define TFT_MISO     HSPI_MISO
  #define TFT_SCK      HSPI_SCK
  #define SPI_BUS_NAME "HSPI"
#endif

// ── Per-driver: constructor, DISP_BEGIN(), color macros ──────────────────

#if defined(DISPLAY_ST7796)
class Configurable_ST7796 : public Arduino_ST7796 {
public:
    Configurable_ST7796(Arduino_DataBus* bus, int8_t rst, bool bgr)
        : Arduino_ST7796(bus, rst), _bgr(bgr) {}

    void setRotation(uint8_t r) override {
        Arduino_TFT::setRotation(r);
        uint8_t madctl = _bgr ? ST7796_MADCTL_BGR : ST7796_MADCTL_RGB;

        switch (_rotation) {
        case 1:
            madctl |= ST7796_MADCTL_MX | ST7796_MADCTL_MV;
            break;
        case 2:
            madctl |= ST7796_MADCTL_MX | ST7796_MADCTL_MY;
            break;
        case 3:
            madctl |= ST7796_MADCTL_MY | ST7796_MADCTL_MV;
            break;
        case 4:
            madctl |= ST7796_MADCTL_MX;
            break;
        case 5:
            madctl |= ST7796_MADCTL_MX | ST7796_MADCTL_MY | ST7796_MADCTL_MV;
            break;
        case 6:
            madctl |= ST7796_MADCTL_MY;
            break;
        case 7:
            madctl |= ST7796_MADCTL_MV;
            break;
        default:
            break;
        }

        _bus->beginWrite();
        _bus->writeC8D8(ST7796_MADCTL, madctl);
        _bus->endWrite();
    }

private:
    bool _bgr;
};
#endif

static Arduino_ESP32SPI displayBus(TFT_DC, TFT_CS, TFT_SCK, TFT_MOSI, TFT_MISO, TFT_SPI_HOST);

#if defined(DISPLAY_ILI9341)
  static Arduino_ILI9341 tft(&displayBus, TFT_RST, 0, false,
                             ILI9341_TFTWIDTH, ILI9341_TFTHEIGHT,
                             0, 0, 0, 0,
                             ili9341_type2_init_operations,
                             sizeof(ili9341_type2_init_operations));
  #define DISP_BEGIN()  tft.begin()
  #define DISP_NAME     "ILI9341"
  #define COLOR_BLACK   RGB565_BLACK
  #define COLOR_WHITE   RGB565_WHITE
  #define COLOR_GREEN   RGB565_GREEN
  #define COLOR_CYAN    RGB565_CYAN
  #define COLOR_YELLOW  RGB565_YELLOW
  #define COLOR_ORANGE  RGB565_ORANGE

#elif defined(DISPLAY_ST7796)
  static Configurable_ST7796 tft(&displayBus, TFT_RST, ST7796_COLOR_ORDER_BGR);
  #define DISP_BEGIN()  tft.begin()
  #define DISP_POST_BEGIN() tft.invertDisplay(!ST7796_INVERT_DISPLAY)
  #define DISP_NAME     "ST7796"
  #define COLOR_BLACK   RGB565_BLACK
  #define COLOR_WHITE   RGB565_WHITE
  #define COLOR_GREEN   RGB565_GREEN
  #define COLOR_CYAN    RGB565_CYAN
  #define COLOR_YELLOW  RGB565_YELLOW
  #define COLOR_ORANGE  RGB565_ORANGE

#elif defined(DISPLAY_ST7735)
  static Arduino_ST7735 tft(&displayBus, TFT_RST, 0, false,
                            ST7735_WIDTH, ST7735_HEIGHT,
                            ST7735_COL_OFFSET1, ST7735_ROW_OFFSET1,
                            ST7735_COL_OFFSET2, ST7735_ROW_OFFSET2,
                            ST7735_BGR);
  #define DISP_BEGIN()  tft.begin()
  #define DISP_NAME     "ST7735"
  #define COLOR_BLACK   RGB565_BLACK
  #define COLOR_WHITE   RGB565_WHITE
  #define COLOR_GREEN   RGB565_GREEN
  #define COLOR_CYAN    RGB565_CYAN
  #define COLOR_YELLOW  RGB565_YELLOW
  #define COLOR_ORANGE  RGB565_ORANGE

#endif  // display selection

#ifndef DISP_POST_BEGIN
  #define DISP_POST_BEGIN() /* nothing */
#endif

// ── Commit shim — no-op for SPI TFTs that draw immediately ───────────────
#ifndef DISP_NEEDS_COMMIT
  #define DISP_NEEDS_COMMIT 0
  #define DISP_COMMIT()     /* nothing */
#endif

// ─── Network / MQTT ──────────────────────────────────────────────────────
static WiFiClient   wifiClient;
static PubSubClient mqtt(wifiClient);

// ─── Runtime state ───────────────────────────────────────────────────────
static uint16_t bgColor  = COLOR_BLACK;
static int16_t  DISP_W   = 0;
static int16_t  DISP_H   = 0;

static char    channels[MAX_CHANNELS][MAX_TOPIC_LEN];
static uint8_t channelCount  = 0;
static int8_t  activeChannel = -1;

static bool     lastSwitchState  = LOW;
static bool     stableSwitchState = LOW;
static uint32_t lastDebounceTime = 0;

static char mqttClientIdBuf[32];
static char errBuf[256];

// ═══════════════════════════════════════════════════════════════════════════
//  VALIDATION HELPERS
// ═══════════════════════════════════════════════════════════════════════════

static inline bool isHexDigit(char c) {
    return (c >= '0' && c <= '9') || (c >= 'A' && c <= 'F') || (c >= 'a' && c <= 'f');
}

static bool validateColorString(const char* s, char* errOut, size_t errLen) {
    if (!s || s[0] != '#') {
        snprintf(errOut, errLen, "color \"%s\" must start with '#'", s ? s : "null");
        return false;
    }
    size_t len = strlen(s + 1);
    if (len != 3 && len != 6) {
        snprintf(errOut, errLen,
                 "color \"%s\": expected 3 or 6 hex digits, got %u", s, (unsigned)len);
        return false;
    }
    for (size_t i = 1; i <= len; ++i) {
        if (!isHexDigit(s[i])) {
            snprintf(errOut, errLen,
                     "color \"%s\": non-hex char '%c' at pos %u", s, s[i], (unsigned)i);
            return false;
        }
    }
    return true;
}

static uint16_t hexToColor565(const char* hex) {
    uint32_t rgb = 0;
    size_t len = strlen(hex + 1);
    if (len == 3) {
        char exp[7];
        snprintf(exp, sizeof(exp), "%c%c%c%c%c%c",
                 hex[1], hex[1], hex[2], hex[2], hex[3], hex[3]);
        rgb = strtoul(exp, nullptr, 16);
    } else {
        rgb = strtoul(hex + 1, nullptr, 16);
    }
    return RGB565((rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF);
}

static bool parseColor(JsonVariant v, const char* field,
                        uint16_t* out, char* errOut, size_t errLen) {
    if (v.isNull()) {
        snprintf(errOut, errLen, "required field \"%s\" (color) is missing", field);
        return false;
    }
    if (v.is<const char*>()) {
        const char* s = v.as<const char*>();
        if (!validateColorString(s, errOut, errLen)) return false;
        *out = hexToColor565(s); return true;
    }
    if (v.is<int>()) { *out = (uint16_t)v.as<int>(); return true; }
    snprintf(errOut, errLen, "field \"%s\" must be a color string or uint16 int", field);
    return false;
}

static bool parseColorOpt(JsonVariant v, const char* field,
                           uint16_t def, uint16_t* out,
                           char* errOut, size_t errLen) {
    if (v.isNull()) { *out = def; return true; }
    return parseColor(v, field, out, errOut, errLen);
}

static bool parseCoord(JsonVariant v, const char* field, int16_t maxVal,
                        int16_t* out, char* errOut, size_t errLen) {
    if (v.isNull()) {
        snprintf(errOut, errLen, "required field \"%s\" (coord) is missing", field);
        return false;
    }
    if (!v.is<int>()) {
        snprintf(errOut, errLen, "field \"%s\" must be an integer [0, %d]",
                 field, (int)maxVal - 1);
        return false;
    }
    int val = v.as<int>();
    if (val < 0 || val >= (int)maxVal) {
        snprintf(errOut, errLen, "field \"%s\"=%d outside bounds [0, %d]",
                 field, val, (int)maxVal - 1);
        return false;
    }
    *out = (int16_t)val; return true;
}

static bool parseDim(JsonVariant v, const char* field,
                      int16_t* out, char* errOut, size_t errLen) {
    if (v.isNull()) {
        snprintf(errOut, errLen, "required field \"%s\" (dim > 0) is missing", field);
        return false;
    }
    if (!v.is<int>()) {
        snprintf(errOut, errLen, "field \"%s\" must be a positive integer", field);
        return false;
    }
    int val = v.as<int>();
    if (val <= 0) {
        snprintf(errOut, errLen, "field \"%s\"=%d must be > 0", field, val);
        return false;
    }
    *out = (int16_t)val; return true;
}

static bool parseFloat(JsonVariant v, const char* field,
                       float* out, char* errOut, size_t errLen) {
    if (v.isNull()) {
        snprintf(errOut, errLen, "required field \"%s\" (number) is missing", field);
        return false;
    }
    if (!v.is<float>() && !v.is<int>()) {
        snprintf(errOut, errLen, "field \"%s\" must be a number", field);
        return false;
    }
    *out = v.as<float>();
    return true;
}

static bool isValidHostname(const char* host) {
    if (!host || host[0] == '\0') return false;
    size_t len = strlen(host);
    if (len > 63 || host[0] == '-' || host[len - 1] == '-') return false;

    for (size_t i = 0; i < len; ++i) {
        char c = host[i];
        bool ok = (c >= 'a' && c <= 'z') ||
                  (c >= 'A' && c <= 'Z') ||
                  (c >= '0' && c <= '9') ||
                  c == '-';
        if (!ok) return false;
    }
    return true;
}

static void configureWiFiStation() {
    WiFi.persistent(false);
    WiFi.disconnect(true, true);
    WiFi.mode(WIFI_OFF);
    delay(100);

    bool hostnameValid = false;
    if (WIFI_HOSTNAME && *WIFI_HOSTNAME) {
        hostnameValid = isValidHostname(WIFI_HOSTNAME);
        if (hostnameValid) {
            WiFi.setHostname(WIFI_HOSTNAME);
        } else {
            Serial.print(F("Invalid hostname, using SDK default: "));
            Serial.println(WIFI_HOSTNAME);
        }
    }

    if (!WiFi.mode(WIFI_STA)) {
        Serial.println(F("WiFi STA mode failed"));
        return;
    }

    if (hostnameValid) {
        bool stationHostOk = WiFi.STA.setHostname(WIFI_HOSTNAME);
        Serial.print(F("Requested DHCP hostname: "));
        Serial.println(WIFI_HOSTNAME);
        Serial.print(F("STA netif hostname: "));
        Serial.println(WiFi.STA.getHostname());
        if (!stationHostOk) {
            Serial.println(F("STA netif hostname request failed"));
        }
    }
}

static uint32_t esp32ChipId24() {
    return (uint32_t)(ESP.getEfuseMac() & 0xFFFFFF);
}

#if TFT_BL_PIN >= 0
static void backlightBegin() {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
    ledcAttach(TFT_BL_PIN, 5000, 8);       // 5 kHz, 8-bit resolution
#else
    ledcSetup(TFT_BL_CHANNEL, 5000, 8);    // 5 kHz, 8-bit resolution
    ledcAttachPin(TFT_BL_PIN, TFT_BL_CHANNEL);
#endif
}

static void backlightWrite(uint8_t value) {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
    ledcWrite(TFT_BL_PIN, value);
#else
    ledcWrite(TFT_BL_CHANNEL, value);
#endif
}
#endif

// ═══════════════════════════════════════════════════════════════════════════
//  STATUS PUBLISHING
// ═══════════════════════════════════════════════════════════════════════════

static void publishStatus(const char* json) {
    if (mqtt.connected()) mqtt.publish(MQTT_STATUS_TOPIC, json);
    Serial.print(F("[STATUS] ")); Serial.println(json);
}

static void publishError(const char* msg) {
    char buf[160], safe[120];
    size_t j = 0;
    for (size_t i = 0; msg[i] && j < sizeof(safe) - 1; ++i)
        safe[j++] = (msg[i] == '"') ? '\'' : msg[i];
    safe[j] = '\0';
    snprintf(buf, sizeof(buf), "{\"event\":\"error\",\"msg\":\"%s\"}", safe);
    publishStatus(buf);
}

// ═══════════════════════════════════════════════════════════════════════════
//  DISPLAY COMMAND HANDLERS
//  Errors → Serial + MQTT status topic.  No TCP socket to reply on.
// ═══════════════════════════════════════════════════════════════════════════

static void dispError(const char* msg) {
    Serial.print(F("[CMD ERR] ")); Serial.println(msg);
    publishError(msg);
}

// ── text ─────────────────────────────────────────────────────────────────
static void cmdText(JsonObject doc) {
    int16_t x, y; uint16_t color;
    if (!parseCoord(doc["x"], "x", DISP_W, &x, errBuf, sizeof(errBuf))) { dispError(errBuf); return; }
    if (!parseCoord(doc["y"], "y", DISP_H, &y, errBuf, sizeof(errBuf))) { dispError(errBuf); return; }
    JsonVariant tv = doc["text"];
    if (tv.isNull())           { dispError("required field \"text\" is missing"); return; }
    if (!tv.is<const char*>()) { dispError("field \"text\" must be a string");    return; }
    const char* text = tv.as<const char*>();
    if (!text || strlen(text) == 0) { dispError("field \"text\" must not be empty"); return; }
    int sz = 1;
    JsonVariant sv = doc["size"];
    if (!sv.isNull()) {
        if (!sv.is<int>()) { dispError("field \"size\" must be an integer in [1,8]"); return; }
        sz = sv.as<int>();
        if (sz < 1 || sz > 8) {
            snprintf(errBuf, sizeof(errBuf), "field \"size\"=%d out of range [1,8]", sz);
            dispError(errBuf); return;
        }
    }
    if (!parseColorOpt(doc["color"], "color", COLOR_WHITE, &color, errBuf, sizeof(errBuf))) {
        dispError(errBuf); return;
    }
    tft.setCursor(x, y);
    tft.setTextColor(color);
    tft.setTextSize((uint8_t)sz);
    tft.setTextWrap(true);
    tft.print(text);
    DISP_COMMIT();
}

// ── clear / bg ────────────────────────────────────────────────────────────
static void cmdClear(JsonObject /*doc*/) {
    tft.fillScreen(bgColor);
    DISP_COMMIT();
}

static void cmdBg(JsonObject doc) {
    uint16_t color;
    if (!parseColor(doc["color"], "color", &color, errBuf, sizeof(errBuf))) {
        dispError(errBuf); return;
    }
    bgColor = color;
}

// ── rect helpers ──────────────────────────────────────────────────────────
static bool extractRect(JsonObject doc,
                         int16_t& x, int16_t& y, int16_t& w, int16_t& h,
                         uint16_t& color) {
    if (!parseCoord(doc["x"], "x", DISP_W, &x, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseCoord(doc["y"], "y", DISP_H, &y, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseDim  (doc["w"], "w",         &w, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseDim  (doc["h"], "h",         &h, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseColor(doc["color"], "color", &color, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    return true;
}

static void cmdFillRect(JsonObject doc) {
    int16_t x, y, w, h; uint16_t c;
    if (!extractRect(doc, x, y, w, h, c)) return;
    tft.fillRect(x, y, w, h, c); DISP_COMMIT();
}

static void cmdRect(JsonObject doc) {
    int16_t x, y, w, h; uint16_t c;
    if (!extractRect(doc, x, y, w, h, c)) return;
    tft.drawRect(x, y, w, h, c); DISP_COMMIT();
}

// ── circle helpers ────────────────────────────────────────────────────────
static bool extractCircle(JsonObject doc,
                            int16_t& x, int16_t& y, int16_t& r, uint16_t& color) {
    if (!parseCoord(doc["x"], "x", DISP_W, &x, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseCoord(doc["y"], "y", DISP_H, &y, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseDim  (doc["r"], "r",         &r, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseColor(doc["color"], "color", &color, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    return true;
}

static void cmdFillCircle(JsonObject doc) {
    int16_t x, y, r; uint16_t c;
    if (!extractCircle(doc, x, y, r, c)) return;
    tft.fillCircle(x, y, r, c); DISP_COMMIT();
}

static void cmdCircle(JsonObject doc) {
    int16_t x, y, r; uint16_t c;
    if (!extractCircle(doc, x, y, r, c)) return;
    tft.drawCircle(x, y, r, c); DISP_COMMIT();
}

// ── ellipse / fill_ellipse ───────────────────────────────────────────────
static bool extractEllipse(JsonObject doc,
                           int16_t& x, int16_t& y,
                           int16_t& rx, int16_t& ry,
                           uint16_t& color) {
    if (!parseCoord(doc["x"], "x", DISP_W, &x, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseCoord(doc["y"], "y", DISP_H, &y, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseDim  (doc["rx"], "rx",       &rx, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseDim  (doc["ry"], "ry",       &ry, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseColor(doc["color"], "color", &color, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    return true;
}

static void cmdEllipse(JsonObject doc) {
    int16_t x, y, rx, ry; uint16_t c;
    if (!extractEllipse(doc, x, y, rx, ry, c)) return;
    tft.drawEllipse(x, y, rx, ry, c); DISP_COMMIT();
}

static void cmdFillEllipse(JsonObject doc) {
    int16_t x, y, rx, ry; uint16_t c;
    if (!extractEllipse(doc, x, y, rx, ry, c)) return;
    tft.fillEllipse(x, y, rx, ry, c); DISP_COMMIT();
}

// ── arc / fill_arc ───────────────────────────────────────────────────────
static bool extractArc(JsonObject doc,
                       int16_t& x, int16_t& y,
                       int16_t& r1, int16_t& r2,
                       float& start, float& end,
                       uint16_t& color) {
    if (!parseCoord(doc["x"], "x", DISP_W, &x, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseCoord(doc["y"], "y", DISP_H, &y, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseDim  (doc["r1"], "r1",       &r1, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseDim  (doc["r2"], "r2",       &r2, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseFloat(doc["start"], "start", &start, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseFloat(doc["end"],   "end",   &end,   errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseColor(doc["color"], "color", &color, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    return true;
}

static void cmdArc(JsonObject doc) {
    int16_t x, y, r1, r2; float start, end; uint16_t c;
    if (!extractArc(doc, x, y, r1, r2, start, end, c)) return;
    tft.drawArc(x, y, r1, r2, start, end, c); DISP_COMMIT();
}

static void cmdFillArc(JsonObject doc) {
    int16_t x, y, r1, r2; float start, end; uint16_t c;
    if (!extractArc(doc, x, y, r1, r2, start, end, c)) return;
    tft.fillArc(x, y, r1, r2, start, end, c); DISP_COMMIT();
}

// ── line primitives ───────────────────────────────────────────────────────
static void cmdHLine(JsonObject doc) {
    int16_t x, y, len; uint16_t c;
    if (!parseCoord(doc["x"],   "x",   DISP_W, &x,   errBuf, sizeof(errBuf))) { dispError(errBuf); return; }
    if (!parseCoord(doc["y"],   "y",   DISP_H, &y,   errBuf, sizeof(errBuf))) { dispError(errBuf); return; }
    if (!parseDim  (doc["len"], "len",          &len, errBuf, sizeof(errBuf))) { dispError(errBuf); return; }
    if (!parseColor(doc["color"], "color", &c,  errBuf, sizeof(errBuf)))       { dispError(errBuf); return; }
    tft.drawFastHLine(x, y, len, c); DISP_COMMIT();
}

static void cmdVLine(JsonObject doc) {
    int16_t x, y, len; uint16_t c;
    if (!parseCoord(doc["x"],   "x",   DISP_W, &x,   errBuf, sizeof(errBuf))) { dispError(errBuf); return; }
    if (!parseCoord(doc["y"],   "y",   DISP_H, &y,   errBuf, sizeof(errBuf))) { dispError(errBuf); return; }
    if (!parseDim  (doc["len"], "len",          &len, errBuf, sizeof(errBuf))) { dispError(errBuf); return; }
    if (!parseColor(doc["color"], "color", &c,  errBuf, sizeof(errBuf)))       { dispError(errBuf); return; }
    tft.drawFastVLine(x, y, len, c); DISP_COMMIT();
}

static void cmdLine(JsonObject doc) {
    int16_t x0, y0, x1, y1; uint16_t c;
    if (!parseCoord(doc["x0"], "x0", DISP_W, &x0, errBuf, sizeof(errBuf))) { dispError(errBuf); return; }
    if (!parseCoord(doc["y0"], "y0", DISP_H, &y0, errBuf, sizeof(errBuf))) { dispError(errBuf); return; }
    if (!parseCoord(doc["x1"], "x1", DISP_W, &x1, errBuf, sizeof(errBuf))) { dispError(errBuf); return; }
    if (!parseCoord(doc["y1"], "y1", DISP_H, &y1, errBuf, sizeof(errBuf))) { dispError(errBuf); return; }
    if (!parseColor(doc["color"], "color", &c, errBuf, sizeof(errBuf)))     { dispError(errBuf); return; }
    tft.drawLine(x0, y0, x1, y1, c); DISP_COMMIT();
}

// ── fill_screen / rotation ────────────────────────────────────────────────
static void cmdFillScreen(JsonObject doc) {
    uint16_t c;
    if (!parseColor(doc["color"], "color", &c, errBuf, sizeof(errBuf))) { dispError(errBuf); return; }
    tft.fillScreen(c); DISP_COMMIT();
}

static void cmdRotation(JsonObject doc) {
    JsonVariant rv = doc["r"];
    if (rv.isNull())   { dispError("field \"r\" (rotation) missing"); return; }
    if (!rv.is<int>()) { dispError("field \"r\" must be 0-3");        return; }
    int rot = rv.as<int>();
    if (rot < 0 || rot > 3) {
        snprintf(errBuf, sizeof(errBuf), "field \"r\"=%d invalid; must be 0-3", rot);
        dispError(errBuf); return;
    }
    tft.setRotation((uint8_t)rot);
    DISP_W = tft.width();
    DISP_H = tft.height();
    Serial.print(F("Rotation=")); Serial.print(rot);
    Serial.print(' '); Serial.print(DISP_W); Serial.print('x'); Serial.println(DISP_H);
    // Publish new dimensions on status topic (no TCP socket to reply on)
    char status[64];
    snprintf(status, sizeof(status),
             "{\"event\":\"rotation\",\"r\":%d,\"w\":%d,\"h\":%d}",
             rot, (int)DISP_W, (int)DISP_H);
    publishStatus(status);
}

// ── pixel ─────────────────────────────────────────────────────────────────
static void cmdPixel(JsonObject doc) {
    int16_t x, y; uint16_t c;
    if (!parseCoord(doc["x"],     "x",     DISP_W, &x, errBuf, sizeof(errBuf))) { dispError(errBuf); return; }
    if (!parseCoord(doc["y"],     "y",     DISP_H, &y, errBuf, sizeof(errBuf))) { dispError(errBuf); return; }
    if (!parseColor(doc["color"], "color", &c,      errBuf, sizeof(errBuf)))     { dispError(errBuf); return; }
    tft.drawPixel(x, y, c); DISP_COMMIT();
}

// ── triangle / fill_triangle ──────────────────────────────────────────────
static bool extractTriangle(JsonObject doc,
                              int16_t& x0, int16_t& y0,
                              int16_t& x1, int16_t& y1,
                              int16_t& x2, int16_t& y2,
                              uint16_t& color) {
    if (!parseCoord(doc["x0"], "x0", DISP_W, &x0, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseCoord(doc["y0"], "y0", DISP_H, &y0, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseCoord(doc["x1"], "x1", DISP_W, &x1, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseCoord(doc["y1"], "y1", DISP_H, &y1, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseCoord(doc["x2"], "x2", DISP_W, &x2, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseCoord(doc["y2"], "y2", DISP_H, &y2, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseColor(doc["color"], "color", &color, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    return true;
}

static void cmdTriangle(JsonObject doc) {
    int16_t x0, y0, x1, y1, x2, y2; uint16_t c;
    if (!extractTriangle(doc, x0, y0, x1, y1, x2, y2, c)) return;
    tft.drawTriangle(x0, y0, x1, y1, x2, y2, c); DISP_COMMIT();
}

static void cmdFillTriangle(JsonObject doc) {
    int16_t x0, y0, x1, y1, x2, y2; uint16_t c;
    if (!extractTriangle(doc, x0, y0, x1, y1, x2, y2, c)) return;
    tft.fillTriangle(x0, y0, x1, y1, x2, y2, c); DISP_COMMIT();
}

// ── rounded_rect / fill_rounded_rect ─────────────────────────────────────
static bool extractRoundedRect(JsonObject doc,
                                 int16_t& x, int16_t& y,
                                 int16_t& w, int16_t& h,
                                 int16_t& r, uint16_t& color) {
    if (!parseCoord(doc["x"], "x", DISP_W, &x, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseCoord(doc["y"], "y", DISP_H, &y, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseDim  (doc["w"], "w",         &w, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseDim  (doc["h"], "h",         &h, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseDim  (doc["r"], "r",         &r, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    if (!parseColor(doc["color"], "color", &color, errBuf, sizeof(errBuf))) { dispError(errBuf); return false; }
    return true;
}

static void cmdRoundedRect(JsonObject doc) {
    int16_t x, y, w, h, r; uint16_t c;
    if (!extractRoundedRect(doc, x, y, w, h, r, c)) return;
    tft.drawRoundRect(x, y, w, h, r, c); DISP_COMMIT();
}

static void cmdFillRoundedRect(JsonObject doc) {
    int16_t x, y, w, h, r; uint16_t c;
    if (!extractRoundedRect(doc, x, y, w, h, r, c)) return;
    tft.fillRoundRect(x, y, w, h, r, c); DISP_COMMIT();
}

// ── brightness ────────────────────────────────────────────────────────────
static void cmdBrightness(JsonObject doc) {
#if TFT_BL_PIN < 0
    dispError("brightness: no backlight pin configured (TFT_BL_PIN is -1)");
#else
    JsonVariant vv = doc["v"];
    if (vv.isNull())   { dispError("required field \"v\" (0-255) is missing"); return; }
    if (!vv.is<int>()) { dispError("field \"v\" must be an integer in [0,255]"); return; }
    int val = vv.as<int>();
    if (val < 0 || val > 255) {
        snprintf(errBuf, sizeof(errBuf), "field \"v\"=%d out of range [0,255]", val);
        dispError(errBuf); return;
    }
    backlightWrite((uint8_t)val);
#endif
}

// ── ping — publishes pong on status topic ─────────────────────────────────
static void cmdPing(JsonObject /*doc*/) {
    char status[64];
    snprintf(status, sizeof(status),
             "{\"event\":\"pong\",\"uptime_ms\":%lu}", millis());
    publishStatus(status);
}

// ── query — publishes display state on status topic ───────────────────────
static void cmdQuery(JsonObject /*doc*/) {
    char status[128];
    snprintf(status, sizeof(status),
             "{\"event\":\"query\",\"w\":%d,\"h\":%d,\"rotation\":%d,"
             "\"bg\":%u,\"free_heap\":%u}",
             (int)DISP_W, (int)DISP_H, (int)tft.getRotation(),
             (unsigned)bgColor, (unsigned)ESP.getFreeHeap());
    publishStatus(status);
}

// ═══════════════════════════════════════════════════════════════════════════
//  DISPATCHER
// ═══════════════════════════════════════════════════════════════════════════

static void dispatchOne(JsonObject obj) {
    JsonVariant cmdv = obj["cmd"];
    if (cmdv.isNull())           { dispError("missing required field \"cmd\""); return; }
    if (!cmdv.is<const char*>()) { dispError("field \"cmd\" must be a string"); return; }
    const char* cmd = cmdv.as<const char*>();
    if (!cmd || !*cmd)           { dispError("field \"cmd\" must not be empty"); return; }

    if      (strcmp(cmd, "text")              == 0) cmdText(obj);
    else if (strcmp(cmd, "clear")             == 0) cmdClear(obj);
    else if (strcmp(cmd, "bg")                == 0) cmdBg(obj);
    else if (strcmp(cmd, "fill_rect")         == 0) cmdFillRect(obj);
    else if (strcmp(cmd, "rect")              == 0) cmdRect(obj);
    else if (strcmp(cmd, "fill_circle")       == 0) cmdFillCircle(obj);
    else if (strcmp(cmd, "circle")            == 0) cmdCircle(obj);
    else if (strcmp(cmd, "ellipse")           == 0) cmdEllipse(obj);
    else if (strcmp(cmd, "fill_ellipse")      == 0) cmdFillEllipse(obj);
    else if (strcmp(cmd, "arc")               == 0) cmdArc(obj);
    else if (strcmp(cmd, "fill_arc")          == 0) cmdFillArc(obj);
    else if (strcmp(cmd, "hline")             == 0) cmdHLine(obj);
    else if (strcmp(cmd, "vline")             == 0) cmdVLine(obj);
    else if (strcmp(cmd, "line")              == 0) cmdLine(obj);
    else if (strcmp(cmd, "fill_screen")       == 0) cmdFillScreen(obj);
    else if (strcmp(cmd, "rotation")          == 0) cmdRotation(obj);
    else if (strcmp(cmd, "pixel")             == 0) cmdPixel(obj);
    else if (strcmp(cmd, "triangle")          == 0) cmdTriangle(obj);
    else if (strcmp(cmd, "fill_triangle")     == 0) cmdFillTriangle(obj);
    else if (strcmp(cmd, "rounded_rect")      == 0) cmdRoundedRect(obj);
    else if (strcmp(cmd, "fill_rounded_rect") == 0) cmdFillRoundedRect(obj);
    else if (strcmp(cmd, "brightness")        == 0) cmdBrightness(obj);
    else if (strcmp(cmd, "ping")              == 0) cmdPing(obj);
    else if (strcmp(cmd, "query")             == 0) cmdQuery(obj);
    else {
        snprintf(errBuf, sizeof(errBuf),
                 "unknown cmd \"%s\"; valid: text|clear|bg|fill_rect|rect|"
                 "fill_circle|circle|ellipse|fill_ellipse|arc|fill_arc|"
                 "hline|vline|line|fill_screen|rotation|"
                 "pixel|triangle|fill_triangle|rounded_rect|fill_rounded_rect|"
                 "brightness|ping|query", cmd);
        dispError(errBuf);
    }
}

static void handleCommand(const char* payload, unsigned int length) {
    if (length >= MAX_PAYLOAD_LEN) {
        snprintf(errBuf, sizeof(errBuf),
                 "MQTT payload %u bytes exceeds MAX_PAYLOAD_LEN (%u) — ignored",
                 (unsigned)length, (unsigned)MAX_PAYLOAD_LEN);
        dispError(errBuf); return;
    }
    static char buf[MAX_PAYLOAD_LEN + 1];
    memcpy(buf, payload, length);
    buf[length] = '\0';

    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, buf);
    if (err) {
        snprintf(errBuf, sizeof(errBuf), "JSON parse error: %s", err.c_str());
        dispError(errBuf); return;
    }

    if (doc.is<JsonArray>()) {
        JsonArray arr = doc.as<JsonArray>();
        uint8_t idx = 0;
        for (JsonVariant item : arr) {
            if (!item.is<JsonObject>()) {
                snprintf(errBuf, sizeof(errBuf),
                         "array element [%u] is not a JSON object — skipped", (unsigned)idx);
                dispError(errBuf);
            } else {
                dispatchOne(item.as<JsonObject>());
            }
            ++idx;
        }
        Serial.print(F("[CMD] array: ")); Serial.print(idx); Serial.println(F(" command(s)"));
    } else if (doc.is<JsonObject>()) {
        dispatchOne(doc.as<JsonObject>());
    } else {
        dispError("payload must be a JSON object {} or array [{},...] of commands");
    }
}

// ═══════════════════════════════════════════════════════════════════════════
//  CHANNEL MANAGEMENT  (unchanged from v1, DISP_COMMIT() added)
// ═══════════════════════════════════════════════════════════════════════════

static void showChannelSplash(const char* name) {
    tft.fillScreen(COLOR_BLACK);
    int16_t bannerY = DISP_H / 3;
    int16_t bannerH = DISP_H / 3;
    tft.fillRect(0, bannerY, DISP_W, bannerH, COLOR_CYAN);
    tft.setTextColor(COLOR_YELLOW);
    tft.setTextSize(1);
    tft.setCursor(4, bannerY - 12);
    tft.print(F("Active channel:"));
    tft.setTextSize(2);
    tft.setTextColor(COLOR_BLACK);
    int16_t tx = 4;
    int16_t nameW = (int16_t)(strlen(name) * 12);
    if (nameW < DISP_W - 8) tx = (DISP_W - nameW) / 2;
    tft.setCursor(tx, bannerY + (bannerH - 16) / 2);
    tft.print(name);
    tft.setTextSize(1);
    tft.setTextColor(COLOR_WHITE);
    tft.setCursor(4, bannerY + bannerH + 4);
    tft.print(F("ch ")); tft.print(activeChannel + 1);
    tft.print(F(" / ")); tft.print(channelCount);
    DISP_COMMIT();
    delay(SPLASH_MS);
    tft.fillScreen(bgColor);
    DISP_COMMIT();
}

static void activateChannel(uint8_t idx) {
    if (idx >= channelCount) return;
    if (activeChannel >= 0 && activeChannel < (int8_t)channelCount) {
        mqtt.unsubscribe(channels[activeChannel]);
        Serial.print(F("Unsubscribed: ")); Serial.println(channels[activeChannel]);
    }
    activeChannel = (int8_t)idx;
    const char* name = channels[activeChannel];
    Serial.print(F("Activating channel: ")); Serial.println(name);
    showChannelSplash(name);
    if (mqtt.subscribe(name)) {
        Serial.print(F("Subscribed: ")); Serial.println(name);
    } else {
        Serial.print(F("Subscribe FAILED: ")); Serial.println(name);
        publishError("MQTT subscribe failed");
    }
    char status[128];
    snprintf(status, sizeof(status),
             "{\"event\":\"channel\",\"name\":\"%s\",\"index\":%d}",
             name, (int)activeChannel);
    publishStatus(status);
}

static void handleControlMessage(const char* payload, unsigned int length) {
    if (length >= 512) { publishError("control payload too large (>511 bytes)"); return; }
    static char buf[512];
    memcpy(buf, payload, length);
    buf[length] = '\0';
    Serial.print(F("[CTRL] ")); Serial.println(buf);

    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, buf);
    if (err || !doc.is<JsonArray>()) {
        publishError("control payload must be a JSON array of channel name strings");
        return;
    }
    JsonArray arr = doc.as<JsonArray>();
    if (arr.size() == 0) { publishError("control channel list is empty"); return; }

    char currentName[MAX_TOPIC_LEN] = "";
    if (activeChannel >= 0 && activeChannel < (int8_t)channelCount)
        strncpy(currentName, channels[activeChannel], sizeof(currentName) - 1);

    channelCount = 0;
    for (JsonVariant v : arr) {
        if (!v.is<const char*>()) continue;
        const char* name = v.as<const char*>();
        if (!name || !*name) continue;
        if (strlen(name) >= MAX_TOPIC_LEN) {
            Serial.print(F("Channel name too long, skipped: ")); Serial.println(name); continue;
        }
        strncpy(channels[channelCount], name, MAX_TOPIC_LEN - 1);
        channels[channelCount][MAX_TOPIC_LEN - 1] = '\0';
        if (++channelCount >= MAX_CHANNELS) break;
    }
    if (channelCount == 0) { publishError("no valid channel names in control list"); return; }

    char status[64];
    snprintf(status, sizeof(status),
             "{\"event\":\"channels\",\"count\":%u}", (unsigned)channelCount);
    publishStatus(status);

    int8_t newIdx = 0;
    if (currentName[0] != '\0') {
        for (uint8_t i = 0; i < channelCount; ++i) {
            if (strcmp(channels[i], currentName) == 0) {
                newIdx = (int8_t)i;
                if (activeChannel == newIdx) return;  // already on this channel
                break;
            }
        }
    }
    activateChannel((uint8_t)newIdx);
}

// ═══════════════════════════════════════════════════════════════════════════
//  MQTT CALLBACK
// ═══════════════════════════════════════════════════════════════════════════

static void mqttCallback(char* topic, byte* payload, unsigned int length) {
    if (strcmp(topic, MQTT_CTRL_TOPIC) == 0)
        handleControlMessage((const char*)payload, length);
    else
        handleCommand((const char*)payload, length);
}

// ═══════════════════════════════════════════════════════════════════════════
//  WIFI & MQTT CONNECTION
// ═══════════════════════════════════════════════════════════════════════════

static void connectWiFi() {
    tft.fillScreen(COLOR_BLACK);
    tft.setTextColor(COLOR_CYAN); tft.setTextSize(1);
    tft.setCursor(2, 2); tft.print(F("Connecting WiFi..."));
    DISP_COMMIT();

    configureWiFiStation();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    Serial.print(F("WiFi"));
    uint8_t dots = 0;
    while (WiFi.status() != WL_CONNECTED) {
        delay(500); Serial.print('.');
        tft.setCursor(2 + dots * 6, 12); tft.print('.');
        DISP_COMMIT();
        dots = (dots + 1) % (DISP_W / 6 - 1);
        yield();
    }
    Serial.println(F(" OK"));
    Serial.print(F("IP: ")); Serial.println(WiFi.localIP());

    tft.fillScreen(COLOR_BLACK); tft.setTextColor(COLOR_GREEN); tft.setTextSize(1);
    tft.setCursor(2,  2); tft.print(F("WiFi connected"));
    tft.setCursor(2, 12); tft.print(WiFi.localIP());
    tft.setCursor(2, 22); tft.print(WiFi.getHostname());
    DISP_COMMIT();
}

static void connectMQTT() {
    const char* clientId = MQTT_CLIENT_ID;
    if (!clientId || !*clientId) {
        snprintf(mqttClientIdBuf, sizeof(mqttClientIdBuf), "tft-%06X", (unsigned)esp32ChipId24());
        clientId = mqttClientIdBuf;
    }
    uint8_t attempt = 0;
    while (!mqtt.connected()) {
        ++attempt;
        Serial.print(F("MQTT connect attempt ")); Serial.println(attempt);
        tft.fillScreen(COLOR_BLACK); tft.setTextColor(COLOR_YELLOW); tft.setTextSize(1);
        tft.setCursor(2,  2); tft.print(F("MQTT connecting..."));
        tft.setCursor(2, 12); tft.print(MQTT_HOST);
        tft.setCursor(2, 22); tft.print(F("Attempt: ")); tft.print(attempt);
        DISP_COMMIT();

        bool ok = (MQTT_USER && *MQTT_USER)
                  ? mqtt.connect(clientId, MQTT_USER, MQTT_PASS)
                  : mqtt.connect(clientId);

        if (ok) {
            Serial.println(F("MQTT connected"));
            mqtt.subscribe(MQTT_CTRL_TOPIC, 1);
            Serial.print(F("Subscribed ctrl: ")); Serial.println(MQTT_CTRL_TOPIC);
            char status[128];
            snprintf(status, sizeof(status),
                     "{\"event\":\"boot\",\"ip\":\"%s\",\"hostname\":\"%s\"}",
                     WiFi.localIP().toString().c_str(), WiFi.getHostname());
            publishStatus(status);
            tft.fillScreen(COLOR_BLACK); tft.setTextColor(COLOR_CYAN); tft.setTextSize(1);
            tft.setCursor(2,  2); tft.print(F("MQTT ready"));
            tft.setCursor(2, 12); tft.print(clientId);
            tft.setCursor(2, 30); tft.setTextColor(COLOR_WHITE);
            tft.print(F("Waiting for"));
            tft.setCursor(2, 40); tft.print(F("channel list..."));
            DISP_COMMIT();
        } else {
            Serial.print(F("MQTT failed, rc=")); Serial.println(mqtt.state());
            uint32_t wait = min((uint32_t)attempt * 3000UL, 30000UL);
            tft.setCursor(2, 32); tft.setTextColor(COLOR_ORANGE);
            tft.print(F("Retry in ")); tft.print(wait / 1000); tft.print(F("s"));
            DISP_COMMIT();
            unsigned long start = millis();
            while (millis() - start < wait) yield();
        }
    }
}

// ═══════════════════════════════════════════════════════════════════════════
//  SWITCH HANDLING  (unchanged from v1)
// ═══════════════════════════════════════════════════════════════════════════

static bool switchPressed() {
    bool reading = digitalRead(SWITCH_PIN);
    if (reading != lastSwitchState) {
        lastDebounceTime = millis();
        lastSwitchState = reading;
    }

    if (millis() - lastDebounceTime <= DEBOUNCE_MS) return false;

    if (reading != stableSwitchState) {
        stableSwitchState = reading;
        return stableSwitchState == HIGH;
    }
    return false;
}

static void nextChannel() {
    if (channelCount == 0) return;
    activateChannel((uint8_t)((activeChannel + 1) % channelCount));
}

// ═══════════════════════════════════════════════════════════════════════════
//  ARDUINO SETUP
// ═══════════════════════════════════════════════════════════════════════════

void setup() {
    Serial.begin(115200);
    Serial.println(F("\r\nESP32 MQTT TFT Terminal v3 (Arduino_GFX ILI9341 / ST7796 / ST7735)"));

    pinMode(SWITCH_GND_PIN, OUTPUT);
    digitalWrite(SWITCH_GND_PIN, LOW);
    pinMode(SWITCH_VDD_PIN, OUTPUT);
    digitalWrite(SWITCH_VDD_PIN, HIGH);
    pinMode(SWITCH_PIN, INPUT);

#if TFT_BL_PIN >= 0
    backlightBegin();
    backlightWrite(255);                  // full brightness on boot
    Serial.print(F("Backlight pin: ")); Serial.println(TFT_BL_PIN);
#endif

    DISP_BEGIN();
    DISP_POST_BEGIN();
    Serial.println(F("Driver: " DISP_NAME));
    Serial.print(F("SPI bus: ")); Serial.println(F(SPI_BUS_NAME));

    tft.setRotation(TFT_ROTATION);
    DISP_W = tft.width();
    DISP_H = tft.height();
    Serial.print(F("Display: ")); Serial.print(DISP_W); Serial.print('x'); Serial.println(DISP_H);

    connectWiFi();

    mqtt.setServer(MQTT_HOST, MQTT_PORT);
    mqtt.setCallback(mqttCallback);
    mqtt.setBufferSize(MAX_PAYLOAD_LEN + 64);

    connectMQTT();
}

// ═══════════════════════════════════════════════════════════════════════════
//  ARDUINO LOOP
// ═══════════════════════════════════════════════════════════════════════════

void loop() {
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println(F("WiFi lost — reconnecting"));
        connectWiFi(); connectMQTT(); return;
    }
    if (!mqtt.connected()) {
        Serial.println(F("MQTT lost — reconnecting"));
        connectMQTT(); return;
    }
    mqtt.loop();
    if (switchPressed()) { Serial.println(F("Switch pressed")); nextChannel(); }
    yield();
}
