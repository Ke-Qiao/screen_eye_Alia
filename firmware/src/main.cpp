#include <Arduino.h>
#include <TFT_eSPI.h>
#include <WiFi.h>
#include <Preferences.h>
#include <cmath>
#include <cstring>
#include "eye_asset.h"
#include "eye_core.h"
#include "network_config.h"

#ifndef EYE_DISPLAY_ENABLED
#define EYE_DISPLAY_ENABLED 1
#endif
constexpr char firmwareVersion[] = "alia-eye-wifi-3";
constexpr uint16_t controlPort = 4242;

extern const uint8_t textureStart[] asm("_binary_data_eye_rgb565_start");
extern const uint8_t textureEnd[] asm("_binary_data_eye_rgb565_end");

static_assert(TFT_WIDTH < TFT_HEIGHT, "The display must be portrait");
static_assert(EYE_CANVAS_WIDTH > TFT_WIDTH && EYE_CANVAS_HEIGHT > TFT_HEIGHT, "Canvas must exceed display in both axes");
static_assert(kTextureWidth <= EYE_CANVAS_WIDTH && kTextureHeight <= EYE_CANVAS_HEIGHT, "Regenerate a texture that fits the canvas");
static_assert(EYE_SPEED > 0, "EYE_SPEED must be positive");
static_assert(EYE_BACKLIGHT_PERCENT >= 0 && EYE_BACKLIGHT_PERCENT <= 100, "Backlight must be 0..100 percent");

#if EYE_DISPLAY_ENABLED
TFT_eSPI tft;
#endif
const eye::Geometry geometry{TFT_WIDTH, TFT_HEIGHT, EYE_CANVAS_WIDTH, EYE_CANVAS_HEIGHT, kTextureWidth, kTextureHeight};
eye::LineBuffer<272> serialInput;
eye::LineBuffer<80> networkInput;
eye::NetworkConfig networkConfig;
WiFiServer server(controlPort, 1);
WiFiClient client;
bool configured = false, networkUp = false, authenticated = false;
uint32_t connectedAt = 0, lastMessage = 0, lastReconnect = 0;
constexpr int stripeRows = 8;
uint16_t stripe[TFT_WIDTH * stripeRows];
int targetX = 0, targetY = 0;
float currentX = 0, currentY = 0;
int drawnX = 0, drawnY = 0;
uint32_t lastFrame = 0;

void drawFrame(int x, int y) {
  // shortcut: blocking SPI stripes, add DMA if measured refresh rate falls short.
#if EYE_DISPLAY_ENABLED
  tft.startWrite();
#endif
  for (int row = 0; row < TFT_HEIGHT; row += stripeRows) {
    const int rows = min(stripeRows, TFT_HEIGHT - row);
    eye::renderRows(geometry, textureStart, x, y, row, rows, EYE_BACKGROUND, stripe);
#if EYE_DISPLAY_ENABLED
    tft.pushImage(0, row, TFT_WIDTH, rows, stripe);
#endif
  }
#if EYE_DISPLAY_ENABLED
  tft.endWrite();
#endif
}

void command(const char* text, Print& output) {
  int requested, x, y;
  if (std::strcmp(text, "INFO") == 0) {
    output.printf("INFO %d %d %d %d\n", TFT_WIDTH, TFT_HEIGHT, geometry.minOffset(), geometry.maxOffset());
  } else if (std::strcmp(text, "INFO2") == 0) {
    output.printf("INFO2 %d %d %d %d %d %d\n", TFT_WIDTH, TFT_HEIGHT,
                  geometry.minX(), geometry.maxX(), geometry.minOffset(), geometry.maxOffset());
  } else if (std::strcmp(text, "STATUS") == 0) {
    output.printf("STATUS %s %s %d %d %d\n", firmwareVersion, WiFi.macAddress().c_str(), EYE_DISPLAY_ENABLED, targetY, drawnY);
  } else if (std::strcmp(text, "POS") == 0) {
    output.printf("POS %d %d %d %d\n", targetX, targetY, drawnX, drawnY);
  } else if (eye::parsePosition(text, x, y)) {
    targetX = geometry.clampX(x);
    targetY = geometry.clamp(y);
    output.printf("OKXY %d %d\n", targetX, targetY);
  } else if (eye::parseOffset(text, requested)) {
    targetY = geometry.clamp(requested);
    output.printf("OK %d\n", targetY);
  } else output.println("ERR command");
}

void startWifi() {
  client.stop();
  server.end();
  networkUp = false;
  WiFi.disconnect(false, false);
  WiFi.begin(networkConfig.ssid, networkConfig.password);
  lastReconnect = millis();
}

void configure(const char* text) {
  eye::NetworkConfig next;
  if (!eye::parseConfig(text, next)) { Serial.println("ERR config"); return; }
  Preferences preferences;
  if (!preferences.begin("eye_net", false)) { Serial.println("ERR storage"); return; }
  const bool saved = preferences.putBytes("config", &next, sizeof(next)) == sizeof(next);
  preferences.end();
  if (!saved) { Serial.println("ERR storage"); return; }
  networkConfig = next;
  configured = true;
  Serial.println("OK CONFIG");
  startWifi();
}

void pollNetwork() {
  if (!configured) return;
  const uint32_t now = millis();
  if (WiFi.status() != WL_CONNECTED) {
    if (networkUp) {
      client.stop();
      server.end();
      networkUp = false;
      Serial.println("NET disconnected");
    }
    if (now - lastReconnect >= 10000) { WiFi.reconnect(); lastReconnect = now; }
    return;
  }
  if (!networkUp) {
    server.begin();
    server.setNoDelay(true);
    networkUp = true;
    Serial.printf("NET %s %u\n", WiFi.localIP().toString().c_str(), controlPort);
  }
  // Arduino 2.0.17 connected() can mistake stale errno for a new socket failure.
  if (client.fd() >= 0 && !client.available() && eye::peerClosed(client.fd())) client.stop();
  if (client.fd() < 0) {
    client.stop();
    client = server.available();
    if (client.fd() < 0) return;
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
  for (unsigned count = 0; count < 128 && client.available(); ++count) {
    const auto result = networkInput.push(char(client.read()));
    if (result == decltype(networkInput)::Pending) continue;
    lastMessage = now;
    if (!authenticated) {
      authenticated = result == decltype(networkInput)::Ready && eye::authorized(networkInput.text, networkConfig.token);
      client.println(authenticated ? "OK AUTH" : "ERR auth");
      if (!authenticated) { client.stop(); return; }
    } else if (result == decltype(networkInput)::Invalid) client.println("ERR line");
    else command(networkInput.text, client);
    std::memset(networkInput.text, 0, sizeof(networkInput.text));
  }
}

void setup() {
  Serial.begin(115200);
  WiFi.persistent(false);
  WiFi.setHostname("alia-eye-esp32");
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.setSleep(false);
  // No wait for the Pi: show the eye as soon as the ESP32 boots.
  if (size_t(textureEnd - textureStart) != size_t(kTextureWidth) * kTextureHeight * 2) {
    Serial.println("ERR asset_size");
    while (true) delay(1000);
  }
#if EYE_DISPLAY_ENABLED
  pinMode(EYE_BACKLIGHT_PIN, OUTPUT);
  digitalWrite(EYE_BACKLIGHT_PIN, LOW);
  if (!ledcSetup(0, 5000, 10)) {
    Serial.println("ERR backlight_pwm");
    while (true) delay(1000);
  }
  ledcWrite(0, 0);
  ledcAttachPin(EYE_BACKLIGHT_PIN, 0);
  tft.init();
  tft.setRotation(0);
  tft.setSwapBytes(true);  // Native RGB565 words -> panel wire byte order.
  if (tft.width() != TFT_WIDTH || tft.height() != TFT_HEIGHT) {
    Serial.println("ERR display_size");
    while (true) delay(1000);
  }
#endif
  drawFrame(0, 0);
#if EYE_DISPLAY_ENABLED
  ledcWrite(0, (1024 * EYE_BACKLIGHT_PERCENT + 50) / 100);
  delay(1);  // LEDC readback reflects the new duty only after the next 5 kHz PWM cycle.
  Serial.printf("BACKLIGHT %d %u %u\n", EYE_BACKLIGHT_PERCENT, ledcRead(0), ledcReadFreq(0));
#endif
  lastFrame = millis();
  command("STATUS", Serial);
  Preferences preferences;
  if (preferences.begin("eye_net", true)) {
    configured = preferences.getBytesLength("config") == sizeof(networkConfig) &&
                 preferences.getBytes("config", &networkConfig, sizeof(networkConfig)) == sizeof(networkConfig) &&
                 eye::validConfig(networkConfig);
    preferences.end();
  }
  if (configured) startWifi();
  else Serial.println("NET unconfigured");
}

void loop() {
  // Bound serial work so an input burst cannot indefinitely starve the display.
  for (unsigned count = 0; count < 64 && Serial.available(); ++count) {
    const auto result = serialInput.push(char(Serial.read()));
    if (result == decltype(serialInput)::Invalid) {
      Serial.println("ERR line");
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
  if (elapsed < 33) { delay(1); return; }
  lastFrame = now;
  const float step = EYE_SPEED * min(elapsed, uint32_t(100)) / 1000.0f;
  if (currentX < targetX) currentX = fminf(targetX, currentX + step);
  else if (currentX > targetX) currentX = fmaxf(targetX, currentX - step);
  if (currentY < targetY) currentY = fminf(targetY, currentY + step);
  else if (currentY > targetY) currentY = fmaxf(targetY, currentY - step);
  const int x = int(lroundf(currentX)), y = int(lroundf(currentY));
  if (x != drawnX || y != drawnY) { drawFrame(x, y); drawnX = x; drawnY = y; }
}
