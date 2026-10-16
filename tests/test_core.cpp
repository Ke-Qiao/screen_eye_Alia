#include "eye_core.h"
#include "eye_asset.h"
#include "display_setup.h"
#include "network_config.h"
#include <algorithm>
#include <cassert>
#include <cstring>
#include <fstream>
#include <iostream>
#include <iterator>
#include <vector>
#include <unistd.h>

std::vector<uint16_t> verify(const eye::Geometry& g, const std::vector<uint8_t>& source, int yOffset, int xOffset = 0) {
  assert(source.size() == size_t(g.textureWidth * g.textureHeight * 2));
  std::vector<uint16_t> world(g.canvasWidth * g.canvasHeight, EYE_BACKGROUND);
  // Independent reference: actually build the full canvas, then take a fixed crop.
  for (int y = 0; y < g.textureHeight; ++y) {
    for (int x = 0; x < g.textureWidth; ++x) {
      const int p = (y * g.textureWidth + x) * 2;
      world[(g.textureY() + g.clamp(yOffset) + y) * g.canvasWidth + g.textureX() + g.clampX(xOffset) + x] = source[p] | (source[p + 1] << 8);
    }
  }
  std::vector<uint16_t> actual(g.width * g.height, 0xDEAD);
  for (int y = 0; y < g.height; y += 8) {
    eye::renderRows(g, source.data(), xOffset, yOffset, y, std::min(8, g.height - y), EYE_BACKGROUND, actual.data() + y * g.width);
  }
  for (int y = 0; y < g.height; ++y)
    for (int x = 0; x < g.width; ++x)
      assert(actual[y * g.width + x] == world[(g.cropY() + y) * g.canvasWidth + g.cropX() + x]);
  return actual;
}

void writeFrame(const char* path, const eye::Geometry& g, const std::vector<uint16_t>& pixels) {
  std::ofstream file(path, std::ios::binary);
  file << "P6\n" << g.width << ' ' << g.height << "\n255\n";
  for (uint16_t pixel : pixels) {
    file.put(char(((pixel >> 11) & 31) * 255 / 31));
    file.put(char(((pixel >> 5) & 63) * 255 / 63));
    file.put(char((pixel & 31) * 255 / 31));
  }
  assert(file.good());
}

void verifyRegions(const eye::Geometry& g, const std::vector<uint8_t>& source) {
  std::vector<uint16_t> display(g.width * g.height, EYE_BACKGROUND), expected(display.size());
  eye::Rect previous{};
  bool first = true;
  for (int centerX : {g.width / 2, 0, g.width - 1})
    for (int angle : {0, 1, -1, 37, 449, 900, -900, 1799, -1800})
      for (int offset : {0, -1, g.minX(), g.maxX(), 0}) {
        const eye::Calibration c{centerX, g.height / 2, angle, 0, 0};
        const auto transform = eye::makeTransform(g, offset, offset / 2, c);
        const auto bounds = eye::textureBounds(g, transform);
        const auto region = first ? eye::Rect{0, 0, g.width, g.height} : eye::unite(previous, bounds);
        assert(region.left >= 0 && region.top >= 0 && region.right <= g.width && region.bottom <= g.height);
        // Frozen v8 sampling reference: the 0-degree copy must retain every pixel.
        for (int y = 0; y < g.height; ++y) {
          float sx = transform.sourceX + y * transform.sine, sy = transform.sourceY + y * transform.cosine;
          for (int x = 0; x < g.width; ++x, sx += transform.cosine, sy -= transform.sine) {
            uint16_t color = EYE_BACKGROUND;
            if (sx >= 0 && sx < g.textureWidth && sy >= 0 && sy < g.textureHeight) {
              const int index = 2 * (int(sy) * g.textureWidth + int(sx));
              color = uint16_t(source[index]) | (uint16_t(source[index + 1]) << 8);
            }
            expected[y * g.width + x] = color;
          }
        }
        for (int y = 0; y < g.height; ++y) for (int x = 0; x < g.width; ++x)
          if (expected[y * g.width + x] != EYE_BACKGROUND)
            assert(x >= bounds.left && x < bounds.right && y >= bounds.top && y < bounds.bottom);
        if (!region.empty()) for (int row = region.top; row < region.bottom; row += 8) {
          const int rows = std::min(8, region.bottom - row);
          std::vector<uint16_t> packed(g.width * rows + 4, 0xDEAD);
          eye::renderRegionRows(g, source.data(), transform, region, row, rows, EYE_BACKGROUND, packed.data());
          for (int i = 0; i < 4; ++i) assert(packed[g.width * rows + i] == 0xDEAD);
          for (int y = 0; y < rows; ++y) for (int x = 0; x < region.width(); ++x)
            display[(row + y) * g.width + region.left + x] = packed[y * region.width() + x];
        }
        assert(display == expected);  // Clear the old position, including clipped/rotated/off-screen eyes.
        previous = bounds;
        first = false;
      }
}

int main() {
  int sockets[2];
  assert(socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0);
  errno = ECONNRESET;
  assert(!eye::peerClosed(sockets[0]));
  assert(send(sockets[1], "X", 1, 0) == 1);
  errno = ECONNRESET;
  assert(!eye::peerClosed(sockets[0]));
  char received;
  assert(recv(sockets[0], &received, 1, 0) == 1 && received == 'X');
  assert(close(sockets[1]) == 0);
  errno = EAGAIN;
  assert(eye::peerClosed(sockets[0]));
  assert(close(sockets[0]) == 0 && eye::peerClosed(-1));
  std::cout << "PASS socket EOF detection ignores stale errno and preserves pending data\n";
  const std::string token(64, 'a');
  eye::NetworkConfig config;
  assert(eye::parseConfig(("CONFIG 746573742077696669 3132333435363738 " + token).c_str(), config));
  assert(std::strcmp(config.ssid, "test wifi") == 0 && std::strcmp(config.password, "12345678") == 0);
  assert(eye::authorized(("AUTH " + token).c_str(), config.token));
  assert(!eye::authorized(("AUTH " + std::string(64, 'b')).c_str(), config.token));
  assert(!eye::authorized("INFO", config.token) && !eye::authorized("AUTH a", config.token));
  assert(!eye::authorized(("AUTH " + token + " extra").c_str(), config.token));
  for (const char* bad : {"", "CONFIG ", "CONFIG 1 3132333435363738 ", "CONFIG 00 3132333435363738 ", "CONFIG zz 3132333435363738 ", "CONFIG 6162 3132 "})
    assert(!eye::parseConfig((std::string(bad) + token).c_str(), config));
  assert(!eye::parseConfig(("CONFIG " + std::string(66, 'a') + " 3132333435363738 " + token).c_str(), config));
  assert(!eye::parseConfig(("CONFIG 6162 " + std::string(130, 'a') + " " + token).c_str(), config));
  assert(!eye::parseConfig(("CONFIG 6162 3132333435363738 " + token + " extra").c_str(), config));
  const std::string largest = "CONFIG " + std::string(64, 'a') + " " + std::string(128, '6') + " " + token;
  assert(eye::parseConfig(largest.c_str(), config));
  std::memset(config.password, 'A', 64);
  assert(eye::validConfig(config));
  eye::LineBuffer<272> provisioning;
  for (char c : largest) assert(provisioning.push(c) == decltype(provisioning)::Pending);
  assert(provisioning.push('\n') == decltype(provisioning)::Ready);
  assert(std::strcmp(provisioning.text, largest.c_str()) == 0);
  std::memset(config.ssid, 'a', sizeof(config.ssid));
  assert(!eye::validConfig(config));
  std::cout << "PASS Wi-Fi configuration bounds, secret framing, authentication and invalid persisted config\n";
  int value = 0;
  for (const char* bad : {"", "Y", "Y ", "Y 1.2", "Y NaN", "Y 1 junk", "X 1", "Y 2147483648", "Y -2147483649", "Y 99999999999999999999999"})
    assert(!eye::parseOffset(bad, value));
  assert(eye::parseOffset("Y -60", value) && value == -60);
  assert(eye::parseOffset("Y 2147483647", value) && value == INT32_MAX);
  assert(eye::parseOffset("Y -2147483648", value) && value == INT32_MIN);
  int horizontal = 7, vertical = 8;
  for (const char* bad : {"", "X", "XY", "XY ", "XY 1", "XY 1 ", "XY 1.2 3", "XY 1 NaN", "XY 1 2 junk",
                         "XY 2147483648 0", "XY 0 -2147483649", "XY 99999999999999999999999 0"}) {
    assert(!eye::parsePosition(bad, horizontal, vertical));
    assert(horizontal == 7 && vertical == 8);
  }
  assert(eye::parsePosition("XY -30 40", horizontal, vertical) && horizontal == -30 && vertical == 40);
  assert(eye::parsePosition("XY -2147483648 2147483647", horizontal, vertical) && horizontal == INT32_MIN && vertical == INT32_MAX);
  int selected = -1;
  const char* inner = nullptr;
  for (const char* bad : {"", "EYE", "EYE ", "EYE 0 XY 0 0", "EYE 3 XY 0 0", "EYE 12 XY 0 0", "EYE 1"}) {
    assert(!eye::parseEyeCommand(bad, selected, inner));
    assert(selected == -1 && inner == nullptr);
  }
  assert(eye::parseEyeCommand("EYE 1 XY -30 40", selected, inner) && selected == 0);
  assert(eye::parsePosition(inner, horizontal, vertical) && horizontal == -30 && vertical == 40);
  assert(eye::parseEyeCommand("EYE 2 Y -60", selected, inner) && selected == 1);
  assert(eye::parseOffset(inner, value) && value == -60);
  assert(eye::parseEyeCommand("EYE 2 POS", selected, inner) && std::strcmp(inner, "POS") == 0);
  eye::LineReader reader;
  for (char c : std::string("Y -60\r")) assert(reader.push(c) == eye::LineReader::Pending);
  assert(reader.push('\n') == eye::LineReader::Ready && std::strcmp(reader.text, "Y -60") == 0);
  for (int i = 0; i < 100; ++i) reader.push('9');
  assert(reader.push('\n') == eye::LineReader::Invalid);
  reader.push('Y'); reader.push(' '); reader.push('1'); reader.push('\0');
  assert(reader.push('\n') == eye::LineReader::Invalid);
  for (char c : std::string("INFO")) reader.push(c);
  assert(reader.push('\n') == eye::LineReader::Ready && std::strcmp(reader.text, "INFO") == 0);

  eye::Calibration calibration{120, 160, 0, 0, 0}, parsed = calibration;
  static_assert(sizeof(eye::CalibrationStore) == 44, "Preserve existing NVS layout");
  const eye::Calibration legacy{133, 151, -73, 1, 1};
  assert(eye::validCalibration(legacy, 240, 320));
  assert(eye::parseCalibration("CAL 239 319 -1800 1 0", parsed, 240, 320));
  assert(parsed.centerX == 239 && parsed.centerY == 319 && parsed.angle10 == -1800 && parsed.flipped == 1);
  assert(eye::parseCalibration("CAL 0 0 1800 0 0", parsed, 240, 320));
  for (const char* bad : {"CAL", "CAL ", "CAL -1 160 0 0 0", "CAL 240 160 0 0 0", "CAL 120 320 0 0 0",
                          "CAL 120 160 1801 0 0", "CAL 120 160 -1801 0 0", "CAL 120 160 0 2 0",
                          "CAL 120 160 0 0 -1", "CAL 120 160 0 0 1", "CAL 120 160 1.2 0 0", "CAL 120 160 0 0 0 junk",
                          "CAL 2147483648 160 0 0 0", "CAL 120 160 0 0", "CAL 120 160 0 0 0 "}) {
    parsed = calibration;
    assert(!eye::parseCalibration(bad, parsed, 240, 320));
    assert(std::memcmp(&parsed, &calibration, sizeof(parsed)) == 0);
  }
  std::cout << "PASS calibration input: strict bounds, tenths of a degree, orientation, rejected color changes, unchanged on rejection\n";

  // Odd dimensions, partial final stripe, exposed background, and exact RGB565 byte order.
  const eye::Geometry small{3, 5, 11, 10, 6, 3};
  std::vector<uint8_t> pattern(6 * 3 * 2);
  for (size_t i = 0; i < pattern.size(); ++i) pattern[i] = uint8_t(i * 13);
  assert(small.minOffset() == -3 && small.maxOffset() == 4);
  assert(small.minX() == -2 && small.maxX() == 3);
  for (int offset : {-1000, small.minOffset(), -1, 0, 1, small.maxOffset(), 1000})
    for (int horizontalOffset : {-1000, small.minX(), 0, small.maxX(), 1000}) verify(small, pattern, offset, horizontalOffset);
  const uint8_t red[] = {0x00, 0xF8};
  const eye::Geometry one{1, 2, 3, 4, 1, 1};
  uint16_t pixel;
  eye::renderRows(one, red, 0, 0, 0, 1, 0xFFFF, &pixel);
  assert(pixel == 0xF800);

  std::ifstream file("firmware/data/eye.rgb565", std::ios::binary);
  assert(file.good());
  const std::vector<uint8_t> image((std::istreambuf_iterator<char>(file)), {});
  const eye::Geometry g{240, 320, 720, 640, kTextureWidth, kTextureHeight};
  verifyRegions(g, image);
  verifyRegions(small, pattern);
  const auto moved = eye::unite(eye::textureBounds(g, eye::makeTransform(g, 0, 0, calibration)),
                                eye::textureBounds(g, eye::makeTransform(g, 1, 0, calibration)));
  assert(moved.width() * moved.height() < g.width * g.height / 2);
  std::cout << "PASS rectangle updates match complete frames pixel-for-pixel: clipping, rotations, old-position clearing, odd widths, partial stripes and buffer bounds\n";
  assert(g.textureWidth <= g.canvasWidth && g.textureHeight <= g.canvasHeight);
  assert(g.minOffset() == -206 && g.maxOffset() == 206);
  assert(g.minX() == -285 && g.maxX() == 285);
  auto center = verify(g, image, 0);
  auto up = verify(g, image, -60);
  auto down = verify(g, image, 60);
  auto left = verify(g, image, 0, -60);
  auto right = verify(g, image, 0, 60);
  assert(center != up && center != down && up != down);
  assert(center != left && center != right && left != right);
  // Independent direction checks catch an axis swap or reversed movement.
  for (int row = 0; row < g.height - 60; ++row)
    for (int x = 0; x < g.width; ++x) {
      assert(up[row * g.width + x] == center[(row + 60) * g.width + x]);
      assert(down[(row + 60) * g.width + x] == center[row * g.width + x]);
    }
  for (int row = 0; row < g.height; ++row)
    for (int x = 0; x < g.width - 60; ++x) {
      assert(left[row * g.width + x] == center[row * g.width + x + 60]);
      assert(right[row * g.width + x + 60] == center[row * g.width + x]);
    }
  for (int offset : {g.minOffset(), 0, g.maxOffset(), -1000, 1000})
    for (int horizontalOffset : {g.minX(), 0, g.maxX(), -1000, 1000}) verify(g, image, offset, horizontalOffset);
  assert(verify(g, image, 0) == center);
  // Independent double-precision inverse rotation checks the actual artwork, not the transform helper.
  for (int angle : {0, 900, -900, 1800, -1800, 1, 123, -164}) {
    const eye::Calibration c{133, 151, angle, 0, 0};
    const auto transform = eye::makeTransform(g, 12, -9, c);
    std::vector<uint16_t> rotated(g.width * g.height);
    for (int row = 0; row < g.height; row += 8)
      eye::renderRows(g, image.data(), transform, row, std::min(8, g.height - row), EYE_BACKGROUND, rotated.data() + row * g.width);
    const double radians = angle * 3.14159265358979323846 / 1800;
    int checked = 0;
    for (int y = 0; y < g.height; ++y) for (int x = 0; x < g.width; ++x) {
      const double dx = x + 0.5 - (c.centerX + 12), dy = y + 0.5 - (c.centerY - 9);
      const double sx = g.textureWidth / 2.0 + std::cos(radians) * dx + std::sin(radians) * dy;
      const double sy = g.textureHeight / 2.0 - std::sin(radians) * dx + std::cos(radians) * dy;
      // Float stepping can choose either neighbor exactly at a pixel boundary.
      if (std::abs(sx - std::round(sx)) < .01 || std::abs(sy - std::round(sy)) < .01) continue;
      uint16_t expected = EYE_BACKGROUND;
      if (sx >= 0 && sx < g.textureWidth && sy >= 0 && sy < g.textureHeight) {
        const int index = (int(sy) * g.textureWidth + int(sx)) * 2;
        expected = image[index] | (image[index+1] << 8);
      }
      assert(rotated[y*g.width+x] == expected);
      ++checked;
    }
    assert(checked > g.width * g.height * .9);
    if (angle == 123) writeFrame(".build/rotated.ppm", g, rotated);
  }
  assert(EYE_BACKGROUND == 0x0000);
  std::cout << "PASS calibration rendering: independent center, clockwise sign, cardinal/fractional angles, stripe continuity and native background\n";
  writeFrame(".build/center.ppm", g, center);
  writeFrame(".build/up.ppm", g, up);
  writeFrame(".build/down.ppm", g, down);
  writeFrame(".build/left.ppm", g, left);
  writeFrame(".build/right.ppm", g, right);
  std::cout << "PASS actual artwork: both axes for portrait mounting, fixed crop, bounds, stripes, RGB565; XY/legacy serial parser and recovery\n";
}
