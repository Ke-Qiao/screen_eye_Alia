#include "st7789_panel.h"
#include <algorithm>
#include <iostream>

int mock::levels[40] = {};
std::vector<unsigned> mock::delays;
mock::Bus mock::buses[4];

const std::vector<uint8_t>& data(uint8_t bus, uint8_t command) {
  const auto& records = mock::buses[bus].records;
  for (auto it = records.rbegin(); it != records.rend(); ++it)
    if (it->command == command) return it->data;
  assert(false && "Missing display command");
  return mock::buses[bus].records.front().data;
}

int main() {
  St7789Panel first(VSPI, EYE1_SCLK, EYE1_MOSI, EYE1_CS, EYE1_DC, EYE1_FLIPPED);
  St7789Panel second(HSPI, EYE2_SCLK, EYE2_MOSI, EYE2_CS, EYE2_DC, EYE2_FLIPPED);
  mock::failInit = 1;
  assert(!first.begin() && !mock::buses[VSPI].initialized);
  mock::failInit = 0;
  assert(first.begin() && second.begin());
  assert(mock::buses[VSPI].sck == 18 && mock::buses[VSPI].mosi == 23 && mock::buses[VSPI].cs == 5);
  assert(mock::buses[HSPI].sck == 14 && mock::buses[HSPI].mosi == 13 && mock::buses[HSPI].cs == 22);
  assert(data(VSPI, 0x36) == std::vector<uint8_t>{0x00});
  assert(data(HSPI, 0x36) == std::vector<uint8_t>{0xC0});
  for (uint8_t bus : {VSPI, HSPI}) {
    assert(mock::buses[bus].miso == -1);
    assert(mock::buses[bus].records.front().command == 0x01);
    const auto& records = mock::buses[bus].records;
    assert(records[records.size() - 3].command == 0x29);
    assert(records[records.size() - 2].command == 0x21);
    assert(records.back().command == 0x36);
    assert(data(bus, 0x3A) == std::vector<uint8_t>{0x55});
    assert(data(bus, 0x21).empty());
  }
  assert(mock::delays == (std::vector<unsigned>{150, 120, 10, 120, 120, 150, 120, 10, 120, 120}));
  alignas(4) uint16_t stripe[240 * 8];
  const auto secondInit = mock::buses[HSPI].records.size();
  first.beginFrame();
  assert(mock::buses[HSPI].records.size() == secondInit);
  for (int row = 0; row < 320; row += 8) {
    for (unsigned i = 0; i < 240 * 8; ++i) stripe[i] = uint16_t(row * 240 + i);
    first.writePixels(stripe, 240 * 8);
    for (unsigned i = 0; i < 240 * 8; ++i) assert(stripe[i] == uint16_t(row * 240 + i));
  }
  first.endFrame();
  assert(mock::buses[HSPI].records.size() == secondInit);
  second.beginFrame();
  std::fill(std::begin(stripe), std::end(stripe), 0xF800);
  for (int row = 0; row < 320; row += 8) second.writePixels(stripe, 240 * 8);
  second.endFrame();
  for (uint8_t bus : {VSPI, HSPI}) {
    assert(data(bus, 0x2A) == (std::vector<uint8_t>{0, 0, 0, 239}));
    assert(data(bus, 0x2B) == (std::vector<uint8_t>{0, 0, 1, 63}));
    assert(data(bus, 0x2C).size() == 240 * 320 * 2);
    assert(!mock::buses[bus].active);
  }
  const auto& frame = data(VSPI, 0x2C);
  const auto& red = data(HSPI, 0x2C);
  for (unsigned i = 0; i < 240 * 320; ++i) {
    assert((frame[2 * i] << 8 | frame[2 * i + 1]) == uint16_t(i));
    assert(red[2 * i] == 0xF8 && red[2 * i + 1] == 0x00);
  }
  const auto secondRecords = mock::buses[HSPI].records.size();
  first.beginFrame();
  for (unsigned i = 0; i < 9; ++i) stripe[i] = uint16_t(0x1234 + i);
  first.writePixels(stripe, 9);  // Pixel count must be converted to bytes, including odd counts.
  first.endFrame();
  assert(mock::buses[HSPI].records.size() == secondRecords);
  const auto& partial = data(VSPI, 0x2C);
  assert(partial.size() == 18);
  for (unsigned i = 0; i < 9; ++i) assert(unsigned(partial[2*i] << 8 | partial[2*i+1]) == 0x1234 + i);
  first.beginFrame(43, 53, 149, 227);
  for (int row = 0; row < 227; row += 8) {
    const unsigned count = 149 * std::min(8, 227 - row);
    for (unsigned i = 0; i < count; ++i) stripe[i] = uint16_t(row * 149 + i);
    first.writePixels(stripe, count);
  }
  first.endFrame();
  assert(data(VSPI, 0x2A) == (std::vector<uint8_t>{0, 43, 0, 191}));
  assert(data(VSPI, 0x2B) == (std::vector<uint8_t>{0, 53, 1, 23}));
  const auto& rectangle = data(VSPI, 0x2C);
  assert(rectangle.size() == 149 * 227 * 2);
  for (unsigned i = 0; i < 149 * 227; ++i)
    assert((rectangle[2*i] << 8 | rectangle[2*i+1]) == uint16_t(i));
  assert(mock::buses[HSPI].records.size() == secondRecords);
  const auto otherCommands = mock::buses[HSPI].records.size();
  first.setFlipped(true);
  assert(data(VSPI, 0x36) == std::vector<uint8_t>{0xC0});
  first.setFlipped(false);
  assert(data(VSPI, 0x36) == std::vector<uint8_t>{0x00});
  for (uint8_t bus : {VSPI, HSPI})
    for (const auto& record : mock::buses[bus].records)
      assert(record.command != 0x20);  // Geometry edits must never switch the accepted color mode.
  assert(mock::buses[HSPI].records.size() == otherCommands);
  std::cout << "PASS dual blocking SPI: independent buses, full/rectangle windows, odd-width RGB565, unchanged source buffer, init failure\n";
}
