#include <Arduino.h>
#include <WiFi.h>
#include <Preferences.h>
#include <esp_wifi.h>
#ifdef EYE_SPI_DRIVE_CAP
#include <driver/gpio.h>
static_assert(EYE_SPI_DRIVE_CAP >= 0 && EYE_SPI_DRIVE_CAP < GPIO_DRIVE_CAP_MAX, "Invalid GPIO drive capability");
#endif
#include <cmath>
#include <cstring>
#include "eye_asset.h"
#include "eye_core.h"
#include "network_config.h"
#include "display_setup.h"

#ifndef EYE_DISPLAY_ENABLED
#define EYE_DISPLAY_ENABLED 1
#endif
#ifndef EYE_WIFI_ENABLED
#define EYE_WIFI_ENABLED 1
#endif
constexpr char firmwareVersion[] = "alia-eye-wifi-10";
constexpr uint16_t controlPort = 4242;

extern const uint8_t textureStart[] asm("_binary_data_eye_rgb565_start");
extern const uint8_t textureEnd[] asm("_binary_data_eye_rgb565_end");

static_assert(TFT_WIDTH < TFT_HEIGHT, "The display must be portrait");
static_assert(EYE_CANVAS_WIDTH > TFT_WIDTH && EYE_CANVAS_HEIGHT > TFT_HEIGHT, "Canvas must exceed display in both axes");
static_assert(kTextureWidth <= EYE_CANVAS_WIDTH && kTextureHeight <= EYE_CANVAS_HEIGHT, "Regenerate a texture that fits the canvas");
static_assert(EYE_SPEED > 0, "EYE_SPEED must be positive");
static_assert(EYE_BACKLIGHT_PERCENT >= 0 && EYE_BACKLIGHT_PERCENT <= 100, "Backlight must be 0..100 percent");

#if EYE_DISPLAY_ENABLED
#include "st7789_panel.h"
#if !CONFIG_IDF_TARGET_ESP32
#error "This wiring and bus configuration requires a classic ESP32, not ESP32-S3/C3"
#endif
St7789Panel panels[] = {
    {VSPI, EYE1_SCLK, EYE1_MOSI, EYE1_CS, EYE1_DC, EYE1_FLIPPED},
    {HSPI, EYE2_SCLK, EYE2_MOSI, EYE2_CS, EYE2_DC, EYE2_FLIPPED},
};
constexpr int backlightPins[] = {EYE1_BL, EYE2_BL};
#endif
const eye::Geometry geometry{TFT_WIDTH, TFT_HEIGHT, EYE_CANVAS_WIDTH, EYE_CANVAS_HEIGHT, kTextureWidth, kTextureHeight};
eye::LineBuffer<272> serialInput;
eye::LineBuffer<80> networkInput;
eye::NetworkConfig networkConfig;
WiFiServer server(controlPort, 1);
WiFiClient client;
bool configured = false, networkUp = false, authenticated = false;
bool serialSync = false, networkSync = false;
uint32_t connectedAt = 0, lastMessage = 0, lastReconnect = 0;
constexpr int stripeRows = 8;
alignas(4) uint16_t stripe[TFT_WIDTH * stripeRows];
struct EyeState {
  int targetX = 0, targetY = 0;
  float currentX = 0, currentY = 0;
  int drawnX = 0, drawnY = 0;
  eye::Rect bounds{};
  bool fullRedraw = true;
  uint32_t frameUs = 0, framePixels = 0, frameNumber = 0;
};
EyeState eyes[2];
uint32_t lastFrame = 0;
const eye::Calibration factoryCalibration[2] = {
    {TFT_WIDTH / 2, TFT_HEIGHT / 2, 0, EYE1_FLIPPED, 0},
    {TFT_WIDTH / 2, TFT_HEIGHT / 2, 0, EYE2_FLIPPED, 0},
};
eye::Calibration calibration[2], savedCalibration[2];
bool calibrationDirty[2] = {false, false};
bool calibrationStored = false;
uint8_t backlight[2] = {EYE_BACKLIGHT_PERCENT, EYE_BACKLIGHT_PERCENT};
uint8_t savedBacklight[2] = {EYE_BACKLIGHT_PERCENT, EYE_BACKLIGHT_PERCENT};

void loadCalibration() {
  std::memcpy(calibration, factoryCalibration, sizeof(calibration));
  Preferences preferences;
  eye::CalibrationStore stored;
  bool loaded = false;
  if (preferences.begin("eye_cal", true)) {
    loaded = preferences.getBytesLength("config") == sizeof(stored) &&
             preferences.getBytes("config", &stored, sizeof(stored)) == sizeof(stored) &&
             stored.magic == eye::calibrationMagic &&
             eye::validCalibration(stored.eyes[0], TFT_WIDTH, TFT_HEIGHT) &&
             eye::validCalibration(stored.eyes[1], TFT_WIDTH, TFT_HEIGHT);
    if (loaded) std::memcpy(calibration, stored.eyes, sizeof(calibration));
    uint8_t brightness[2];
    if (preferences.getBytesLength("backlight") == sizeof(brightness) &&
        preferences.getBytes("backlight", brightness, sizeof(brightness)) == sizeof(brightness) &&
        brightness[0] <= 100 && brightness[1] <= 100)
      std::memcpy(backlight, brightness, sizeof(backlight));
    preferences.end();
  }
  std::memcpy(savedCalibration, calibration, sizeof(calibration));
  std::memcpy(savedBacklight, backlight, sizeof(backlight));
  // Keep existing geometry while retiring v6's whole-panel inversion setting.
  for (auto& c : calibration) c.reserved = 0;
  calibrationStored = loaded;
  Serial.printf("CALSTORE %s\n", loaded ? "loaded" : "default");
}

void applyBacklight(int i, int percent) {
  backlight[i] = uint8_t(percent);
#if EYE_DISPLAY_ENABLED
  ledcWrite(i, eye::backlightDuty(percent));
  delay(1);  // Let the next PWM cycle latch the duty before reporting completion.
#endif
}

void applyCalibration(int i, const eye::Calibration& next) {
  if (next.flipped != calibration[i].flipped) eyes[i].fullRedraw = true;
#if EYE_DISPLAY_ENABLED
  if (next.flipped != calibration[i].flipped) panels[i].setFlipped(next.flipped);
#endif
  if (std::memcmp(&next, &calibration[i], sizeof(next)) != 0) calibrationDirty[i] = true;
  calibration[i] = next;
}


void drawFrames(const int x[2], const int y[2], bool full = false) {
  // shortcut: no TE signal is wired; smaller blocking frames reduce latency but cannot guarantee vsync.
  for (int i = 0; i < 2; ++i) {
    if (!full && !calibrationDirty[i] && x[i] == eyes[i].drawnX && y[i] == eyes[i].drawnY) continue;
    const auto transform = eye::makeTransform(geometry, x[i], y[i], calibration[i]);
    const auto bounds = eye::textureBounds(geometry, transform);
    const auto region = full || eyes[i].fullRedraw ? eye::Rect{0, 0, TFT_WIDTH, TFT_HEIGHT} : eye::unite(eyes[i].bounds, bounds);
    const uint32_t started = micros();
    if (!region.empty()) {
#if EYE_DISPLAY_ENABLED
      panels[i].beginFrame(region.left, region.top, region.width(), region.height());
#endif
      for (int row = region.top; row < region.bottom; row += stripeRows) {
        const int rows = min(stripeRows, region.bottom - row);
        eye::renderRegionRows(geometry, textureStart, transform, region, row, rows, EYE_BACKGROUND, stripe);
#if EYE_DISPLAY_ENABLED
        panels[i].writePixels(stripe, region.width() * rows);
#endif
      }
#if EYE_DISPLAY_ENABLED
      panels[i].endFrame();
#endif
    }
    eyes[i].frameUs = micros() - started;
    eyes[i].framePixels = region.empty() ? 0 : region.width() * region.height();
    ++eyes[i].frameNumber;
    eyes[i].bounds = bounds;
    eyes[i].fullRedraw = false;
    eyes[i].drawnX = x[i];
    eyes[i].drawnY = y[i];
    calibrationDirty[i] = false;
  }
}

void command(const char* text, Print& output) {
  int requested, x, y;
  eye::Calibration next;
  int selected = -1;
  if (std::strncmp(text, "EYE ", 4) == 0 && !eye::parseEyeCommand(text, selected, text)) {
    output.print("ERR command\r\n");
    return;
  }
  const int first = selected < 0 ? 0 : selected;
  const int end = selected < 0 ? 2 : selected + 1;
  if (selected < 0 && std::strcmp(text, "EYES") == 0) {
    output.print("EYES 2\r\n");
  } else if (selected < 0 && std::strcmp(text, "SYNC") == 0) {
    if (&output == &Serial) serialSync = true;
    else networkSync = true;
  } else if (selected < 0 && std::strcmp(text, "INFO") == 0) {
    output.printf("INFO %d %d %d %d\n", TFT_WIDTH, TFT_HEIGHT, geometry.minOffset(), geometry.maxOffset());
  } else if (selected < 0 && std::strcmp(text, "INFO2") == 0) {
    output.printf("INFO2 %d %d %d %d %d %d\n", TFT_WIDTH, TFT_HEIGHT,
                  geometry.minX(), geometry.maxX(), geometry.minOffset(), geometry.maxOffset());
  } else if (selected < 0 && std::strcmp(text, "STATUS") == 0) {
    output.printf("STATUS %s %s %d %d %d\n", firmwareVersion, WiFi.macAddress().c_str(), EYE_DISPLAY_ENABLED, eyes[0].targetY, eyes[0].drawnY);
  } else if (selected < 0 && std::strcmp(text, "LINK") == 0) {
    wifi_ap_record_t ap = {};
    wifi_ps_type_t powerSave = WIFI_PS_NONE;
    const int apError = esp_wifi_sta_get_ap_info(&ap);
    const int psError = esp_wifi_get_ps(&powerSave);
    output.printf("LINK %lu %d %d %d %u %02X:%02X:%02X:%02X:%02X:%02X %d %d %u %u\n",
                  millis(), int(WiFi.status()), apError, int(ap.rssi), unsigned(ap.primary),
                  ap.bssid[0], ap.bssid[1], ap.bssid[2], ap.bssid[3], ap.bssid[4], ap.bssid[5],
                  psError, int(powerSave), unsigned(ESP.getFreeHeap()), unsigned(ESP.getMinFreeHeap()));
  } else if (selected >= 0 && std::strcmp(text, "CAL") == 0) {
    const auto& c = calibration[selected];
    output.printf("CAL %d %d %d %d %d %d %d\n", selected + 1, int(c.centerX), int(c.centerY),
                  int(c.angle10), int(c.flipped), int(c.reserved),
                  !calibrationStored || std::memcmp(&c, &savedCalibration[selected], sizeof(c)) != 0 ||
                  backlight[selected] != savedBacklight[selected]);
  } else if (selected >= 0 && std::strcmp(text, "BL") == 0) {
    unsigned duty = eye::backlightDuty(backlight[selected]);
#if EYE_DISPLAY_ENABLED
    duty = ledcRead(selected);
#endif
    output.printf("BL %d %u %u\n", selected + 1, unsigned(backlight[selected]), duty);
  } else if (selected >= 0 && eye::parseBacklight(text, requested)) {
    applyBacklight(selected, requested);
    output.printf("OKBL %d %d\n", selected + 1, requested);
  } else if (selected >= 0 && std::strcmp(text, "FRAME") == 0) {
    output.printf("FRAME %d %lu %lu %lu\n", selected + 1, eyes[selected].frameUs,
                  eyes[selected].framePixels, eyes[selected].frameNumber);
  } else if (selected >= 0 && eye::parseCalibration(text, next, TFT_WIDTH, TFT_HEIGHT)) {
    applyCalibration(selected, next);
    output.printf("OKCAL %d\n", selected + 1);
  } else if (selected >= 0 && std::strcmp(text, "RESETCAL") == 0) {
    applyCalibration(selected, factoryCalibration[selected]);
    applyBacklight(selected, EYE_BACKLIGHT_PERCENT);
    output.printf("OKCAL %d\n", selected + 1);
  } else if (selected < 0 && std::strcmp(text, "SAVECAL") == 0) {
    const bool geometryChanged = !calibrationStored || std::memcmp(calibration, savedCalibration, sizeof(calibration)) != 0;
    const bool lightChanged = std::memcmp(backlight, savedBacklight, sizeof(backlight)) != 0;
    if (!geometryChanged && !lightChanged) {
      output.print("OKSAVE\r\n");
      return;
    }
    Preferences preferences;
    if (!preferences.begin("eye_cal", false)) { output.print("ERR storage\r\n"); return; }
    eye::CalibrationStore stored;
    stored.magic = eye::calibrationMagic;
    std::memcpy(stored.eyes, calibration, sizeof(calibration));
    bool saved = true;
    if (geometryChanged) {
      saved = preferences.putBytes("config", &stored, sizeof(stored)) == sizeof(stored);
      if (saved) {
        std::memcpy(savedCalibration, calibration, sizeof(calibration));
        calibrationStored = true;
      }
    }
    if (saved && lightChanged) {
      saved = preferences.putBytes("backlight", backlight, sizeof(backlight)) == sizeof(backlight);
      if (saved) std::memcpy(savedBacklight, backlight, sizeof(backlight));
    }
    preferences.end();
    if (!saved) { output.print("ERR storage\r\n"); return; }
    output.print("OKSAVE\r\n");
  } else if (std::strcmp(text, "POS") == 0) {
    const auto& state = eyes[first];
    if (selected >= 0) output.printf("POS %d %d %d %d %d\n", selected + 1,
                                    state.targetX, state.targetY, state.drawnX, state.drawnY);
    else output.printf("POS %d %d %d %d\n", state.targetX, state.targetY, state.drawnX, state.drawnY);
  } else if (eye::parsePosition(text, x, y)) {
    for (int i = first; i < end; ++i) {
      eyes[i].targetX = geometry.clampX(x);
      eyes[i].targetY = geometry.clamp(y);
    }
    if (selected >= 0) output.printf("OKXY %d %d %d\n", selected + 1, eyes[first].targetX, eyes[first].targetY);
    else output.printf("OKXY %d %d\n", eyes[first].targetX, eyes[first].targetY);
  } else if (eye::parseOffset(text, requested)) {
    for (int i = first; i < end; ++i) eyes[i].targetY = geometry.clamp(requested);
    if (selected >= 0) output.printf("OK %d %d\n", selected + 1, eyes[first].targetY);
    else output.printf("OK %d\n", eyes[first].targetY);
  } else output.print("ERR command\r\n");
}

void startWifi() {
  if (!EYE_WIFI_ENABLED) return;
  client.stop();
  server.end();
  networkUp = false;
  WiFi.disconnect(false, false);
  WiFi.begin(networkConfig.ssid, networkConfig.password);
  lastReconnect = millis();
}

void configure(const char* text) {
  if (!EYE_WIFI_ENABLED) { Serial.print("ERR wifi_disabled\r\n"); return; }
  eye::NetworkConfig next;
  if (!eye::parseConfig(text, next)) { Serial.print("ERR config\r\n"); return; }
  Preferences preferences;
  if (!preferences.begin("eye_net", false)) { Serial.print("ERR storage\r\n"); return; }
  const bool saved = preferences.putBytes("config", &next, sizeof(next)) == sizeof(next);
  preferences.end();
  if (!saved) { Serial.print("ERR storage\r\n"); return; }
  networkConfig = next;
  configured = true;
  Serial.print("OK CONFIG\r\n");
  startWifi();
}

void pollNetwork() {
  if (!EYE_WIFI_ENABLED || !configured) return;
  const uint32_t now = millis();
  if (WiFi.status() != WL_CONNECTED) {
    if (networkUp) {
      client.stop();
      server.end();
      networkUp = false;
      Serial.print("NET disconnected\r\n");
    }
    if (now - lastReconnect >= 10000) { WiFi.reconnect(); lastReconnect = now; }
    return;
  }
  if (!networkUp) {
    server.begin();
    server.setNoDelay(true);
    networkUp = true;
    Serial.printf("NET %s %u\n", WiFi.localIP().toString().c_str(), controlPort);
    command("LINK", Serial);
  }
  // Arduino 2.0.17 connected() can mistake stale errno for a new socket failure.
  if (client.fd() >= 0 && !client.available() && eye::peerClosed(client.fd())) client.stop();
  if (client.fd() < 0) {
    client.stop();
    client = server.available();
    if (client.fd() < 0) return;
    networkSync = false;
    client.setNoDelay(true);
    client.setTimeout(1);
    authenticated = false;
    networkInput = {};
    connectedAt = lastMessage = now;
  }
  if ((!authenticated && now - connectedAt >= 3000) || now - lastMessage >= 12000) {
    client.stop();
    return;
  }
  for (unsigned count = 0; count < 128 && !networkSync && client.available(); ++count) {
    const auto result = networkInput.push(char(client.read()));
    if (result == decltype(networkInput)::Pending) continue;
    lastMessage = now;
    if (!authenticated) {
      authenticated = result == decltype(networkInput)::Ready && eye::authorized(networkInput.text, networkConfig.token);
      client.print(authenticated ? "OK AUTH\r\n" : "ERR auth\r\n");
      if (!authenticated) { client.stop(); return; }
    } else if (result == decltype(networkInput)::Invalid) client.print("ERR line\r\n");
    else command(networkInput.text, client);
    std::memset(networkInput.text, 0, sizeof(networkInput.text));
  }
}

void setup() {
  Serial.begin(115200);

#if EYE_WIFI_ENABLED
  WiFi.persistent(false);
  WiFi.setHostname("alia-eye-esp32");
  WiFi.onEvent([](WiFiEvent_t event, WiFiEventInfo_t info) {
    if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED)
      Serial.printf("WIFI_EVENT %lu DOWN %u\n", millis(), unsigned(info.wifi_sta_disconnected.reason));
    else if (event == ARDUINO_EVENT_WIFI_STA_CONNECTED)
      Serial.printf("WIFI_EVENT %lu UP\n", millis());
  });
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.setSleep(false);
#else
  wifi_mode_t mode;
  if (esp_wifi_get_mode(&mode) != ESP_ERR_WIFI_NOT_INIT) {
    Serial.print("ERR wifi_initialized\r\n");
    while (true) delay(1000);
  }
  Serial.print("RADIO disabled\r\n");
#endif
  loadCalibration();
  // No wait for the Pi: show the eye as soon as the ESP32 boots.
  if (size_t(textureEnd - textureStart) != size_t(kTextureWidth) * kTextureHeight * 2) {
    Serial.print("ERR asset_size\r\n");
    while (true) delay(1000);
  }
#if EYE_DISPLAY_ENABLED
  for (int i = 0; i < 2; ++i) {
    pinMode(backlightPins[i], OUTPUT);
    digitalWrite(backlightPins[i], LOW);
    if (!ledcSetup(i, 5000, 10)) {
      Serial.print("ERR backlight_pwm\r\n");
      while (true) delay(1000);
    }
    ledcWrite(i, 0);
    ledcAttachPin(backlightPins[i], i);
  }
  for (int i = 0; i < 2; ++i) {
    if (!panels[i].begin()) {
      Serial.printf("ERR display_spi %d\n", i + 1);
      while (true) delay(1000);
    }
    panels[i].setFlipped(calibration[i].flipped);
  }
#endif
#if EYE_DISPLAY_ENABLED && defined(EYE_SPI_DRIVE_CAP)
  // Diagnostic only: vary pad drive independently of SPI clock and display commands.
  for (int pin : {EYE1_SCLK, EYE1_MOSI, EYE1_CS, EYE1_DC, EYE2_SCLK, EYE2_MOSI, EYE2_CS, EYE2_DC}) {
    gpio_drive_cap_t before, after;
    if (gpio_get_drive_capability(gpio_num_t(pin), &before) != ESP_OK ||
        gpio_set_drive_capability(gpio_num_t(pin), gpio_drive_cap_t(EYE_SPI_DRIVE_CAP)) != ESP_OK ||
        gpio_get_drive_capability(gpio_num_t(pin), &after) != ESP_OK || int(after) != EYE_SPI_DRIVE_CAP) {
      Serial.printf("ERR spi_drive %d\n", pin);
      while (true) delay(1000);
    }
    Serial.printf("SPI_DRIVE %d %d %d\n", pin, int(before), int(after));
  }
#endif
  const int centered[2] = {0, 0};
  drawFrames(centered, centered, true);
#if EYE_DISPLAY_ENABLED
  for (int i = 0; i < 2; ++i) ledcWrite(i, eye::backlightDuty(backlight[i]));
  delay(1);  // LEDC readback reflects the new duty only after the next 5 kHz PWM cycle.
  for (int i = 0; i < 2; ++i)
    Serial.printf("BACKLIGHT %d %u %u %u\n", i + 1, unsigned(backlight[i]), ledcRead(i), ledcReadFreq(i));
  Serial.printf("DISPLAY dual-blocking %u %u %u\n", SPI_FREQUENCY, unsigned(sizeof(stripe)), unsigned(EYE_SPEED));
  Serial.printf("MOUNT portrait %d %d %d %d\n", int(calibration[0].flipped), int(calibration[1].flipped), kTextureWidth, kTextureHeight);
#endif
  lastFrame = millis();
  command("STATUS", Serial);
  command("EYE 1 CAL", Serial);
  command("EYE 2 CAL", Serial);
#if EYE_WIFI_ENABLED
  Preferences preferences;
  if (preferences.begin("eye_net", true)) {
    configured = preferences.getBytesLength("config") == sizeof(networkConfig) &&
                 preferences.getBytes("config", &networkConfig, sizeof(networkConfig)) == sizeof(networkConfig) &&
                 eye::validConfig(networkConfig);
    preferences.end();
  }
  if (configured) startWifi();
  else Serial.print("NET unconfigured\r\n");
#endif
}

void loop() {
  // Bound serial work so an input burst cannot indefinitely starve the display.
  for (unsigned count = 0; count < 64 && !serialSync && Serial.available(); ++count) {
    const auto result = serialInput.push(char(Serial.read()));
    if (result == decltype(serialInput)::Invalid) {
      Serial.print("ERR line\r\n");
      std::memset(serialInput.text, 0, sizeof(serialInput.text));
    } else if (result == decltype(serialInput)::Ready) {
      if (std::strncmp(serialInput.text, "CONFIG ", 7) == 0) configure(serialInput.text);
      else command(serialInput.text, Serial);
      std::memset(serialInput.text, 0, sizeof(serialInput.text));
    }
  }
  pollNetwork();
  const uint32_t now = millis();
  const uint32_t elapsed = now - lastFrame;  // Handles millis() wraparound.
  if (elapsed < 33 && !calibrationDirty[0] && !calibrationDirty[1]) { delay(1); return; }
  lastFrame = now;
  const float step = EYE_SPEED * min(elapsed, uint32_t(100)) / 1000.0f;
  int x[2], y[2];
  for (int i = 0; i < 2; ++i) {
    auto& state = eyes[i];
    if (state.currentX < state.targetX) state.currentX = fminf(state.targetX, state.currentX + step);
    else if (state.currentX > state.targetX) state.currentX = fmaxf(state.targetX, state.currentX - step);
    if (state.currentY < state.targetY) state.currentY = fminf(state.targetY, state.currentY + step);
    else if (state.currentY > state.targetY) state.currentY = fmaxf(state.targetY, state.currentY - step);
    x[i] = int(lroundf(state.currentX));
    y[i] = int(lroundf(state.currentY));
  }
  drawFrames(x, y);
  // Reply only after this frame is sent; later commands stay in their input stream.
  if (serialSync) Serial.print("OKSYNC\r\n");
  if (networkSync && authenticated && client.fd() >= 0) client.print("OKSYNC\r\n");
  serialSync = networkSync = false;
}
