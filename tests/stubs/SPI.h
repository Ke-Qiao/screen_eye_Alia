#pragma once
#include "Arduino.h"

constexpr uint8_t HSPI = 2, VSPI = 3, MSBFIRST = 1, SPI_MODE3 = 0x11;
struct SPISettings {
  uint32_t frequency;
  uint8_t order, mode;
  SPISettings(uint32_t f, uint8_t o, uint8_t m) : frequency(f), order(o), mode(m) {}
};
namespace mock {
struct Record { uint8_t command; std::vector<uint8_t> data; };
struct Bus {
  int sck = -1, miso = -1, mosi = -1, cs = -1, dc = -1;
  bool initialized = false, active = false;
  std::vector<Record> records;
};
extern Bus buses[4];
inline bool failInit = false;
}
class SPIClass {
  mock::Bus& b;
 public:
  explicit SPIClass(uint8_t host) : b(mock::buses[host]) {
    assert(host == HSPI || host == VSPI);
    b.dc = host == VSPI ? 27 : 32;
  }
  void begin(int sck, int miso, int mosi, int cs) {
    if (mock::failInit) return;
    b.sck = sck; b.miso = miso; b.mosi = mosi; b.cs = cs;
    b.initialized = true;
  }
  void* bus() { return b.initialized ? &b : nullptr; }
  void beginTransaction(SPISettings settings) {
    assert(b.initialized && settings.frequency == 10000000);
    assert(settings.order == MSBFIRST && settings.mode == SPI_MODE3);
    for (const auto& other : mock::buses) assert(!other.active);
    assert(mock::levels[b.cs] == HIGH);
    b.active = true;
  }
  void endTransaction() {
    assert(b.active && mock::levels[b.cs] == HIGH);
    b.active = false;
  }
  void write(uint8_t value) {
    assert(b.active && mock::levels[b.cs] == LOW);
    if (mock::levels[b.dc] == LOW) b.records.push_back({value, {}});
    else { assert(!b.records.empty()); b.records.back().data.push_back(value); }
  }
  void write16(uint16_t value) { write(value >> 8); write(value & 255); }
  void writePixels(const void* buffer, uint32_t bytes) {
    assert(bytes > 0 && bytes % 2 == 0 && bytes <= 240 * 8 * 2);
    assert(mock::levels[b.dc] == HIGH && b.records.back().command == 0x2C);
    const auto* pixels = static_cast<const uint16_t*>(buffer);
    for (uint32_t i = 0; i < bytes / 2; ++i) write16(pixels[i]);
  }
};
