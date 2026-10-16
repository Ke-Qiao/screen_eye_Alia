#pragma once
#include <Arduino.h>
#include <SPI.h>
#include <cassert>
#include <initializer_list>
#include "display_setup.h"

// Keep the two physical SPI buses independent; complete each frame before the next.
class St7789Panel {
 public:
  St7789Panel(uint8_t bus, int sck, int mosi, int cs, int dc, bool flipped)
      : spi_(bus), sck_(sck), mosi_(mosi), cs_(cs), dc_(dc), flipped_(flipped) {}

  bool begin() {
    pinMode(cs_, OUTPUT);
    digitalWrite(cs_, HIGH);
    pinMode(dc_, OUTPUT);
    digitalWrite(dc_, HIGH);
    spi_.begin(sck_, -1, mosi_, cs_);
    if (!spi_.bus()) return false;
    send(0x01);  // Software reset: these modules have no reset pin.
    delay(150);
    send(0x11);  // Sleep out.
    delay(120);
    send(0x13);  // Normal mode.
    send(0x36, {uint8_t(flipped_ ? 0xC0 : 0x00)});  // MX|MY = 180 degrees; RGB order.
    // Preserve the working TFT_eSPI 2.5.43 ST7789 register values (see NOTICE).
    send(0xB6, {0x0A, 0x82});
    send(0xB0, {0x00, 0xE0});
    send(0x3A, {0x55});  // RGB565.
    delay(10);
    send(0xB2, {0x0C, 0x0C, 0x00, 0x33, 0x33});
    send(0xB7, {0x35});
    send(0xBB, {0x28});
    send(0xC0, {0x0C});
    send(0xC2, {0x01, 0xFF});
    send(0xC3, {0x10});
    send(0xC4, {0x20});
    send(0xC6, {0x0F});
    send(0xD0, {0xA4, 0xA1});
    send(0xE0, {0xD0, 0x00, 0x02, 0x07, 0x0A, 0x28, 0x32, 0x44, 0x42, 0x06, 0x0E, 0x12, 0x14, 0x17});
    send(0xE1, {0xD0, 0x00, 0x02, 0x07, 0x0A, 0x28, 0x31, 0x54, 0x47, 0x0E, 0x1C, 0x17, 0x1B, 0x1E});
    send(0x21);  // Inversion on, as on the original panel.
    delay(120);
    send(0x29);  // Display on; application keeps BL off until the first frame.
    delay(120);
    // Match the working driver: reapply inversion and addressing after DISPON.
    send(0x21);
    send(0x36, {uint8_t(flipped_ ? 0xC0 : 0x00)});
    return true;
  }

  void setFlipped(bool flipped) {
    flipped_ = flipped;
    send(0x36, {uint8_t(flipped ? 0xC0 : 0x00)});
  }

  void beginFrame(int left = 0, int top = 0, int width = TFT_WIDTH, int height = TFT_HEIGHT) {
    assert(left >= 0 && top >= 0 && width > 0 && height > 0);
    assert(left + width <= TFT_WIDTH && top + height <= TFT_HEIGHT);
    startWrite();
    command(0x2A);
    word(left);
    word(left + width - 1);
    command(0x2B);
    word(top);
    word(top + height - 1);
    command(0x2C);
  }

  void writePixels(const uint16_t* pixels, unsigned count) {
    assert(count > 0 && count <= TFT_WIDTH * 8);
    // Arduino writes RGB565 high byte first and returns after transmission.
    spi_.writePixels(pixels, count * sizeof(uint16_t));
  }

  void endFrame() {
    digitalWrite(cs_, HIGH);
    spi_.endTransaction();
  }

 private:
  SPIClass spi_;
  int sck_, mosi_, cs_, dc_;
  bool flipped_;

  void startWrite() {
    spi_.beginTransaction(SPISettings(SPI_FREQUENCY, MSBFIRST, SPI_MODE3));
    digitalWrite(cs_, LOW);
  }

  void command(uint8_t value) {
    digitalWrite(dc_, LOW);
    byte(value);
    digitalWrite(dc_, HIGH);
  }

  void byte(uint8_t value) {
    spi_.write(value);
  }

  void word(uint16_t value) {
    spi_.write16(value);
  }

  void send(uint8_t value, std::initializer_list<uint8_t> data = {}) {
    startWrite();
    command(value);
    for (uint8_t value : data) byte(value);
    endFrame();
  }
};
