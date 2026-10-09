#include "eye_core.h"
#include "eye_asset.h"
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
  std::vector<uint16_t> world(g.canvasWidth * g.canvasHeight, 0xFFFF);
  // Independent reference: actually build the full canvas, then take a fixed crop.
  for (int y = 0; y < g.textureHeight; ++y) {
    for (int x = 0; x < g.textureWidth; ++x) {
      const int p = (y * g.textureWidth + x) * 2;
      world[(g.textureY() - g.clampX(xOffset) + y) * g.canvasWidth + g.textureX() + g.clamp(yOffset) + x] = source[p] | (source[p + 1] << 8);
    }
  }
  std::vector<uint16_t> actual(g.width * g.height, 0xDEAD);
  for (int y = 0; y < g.height; y += 8) {
    eye::renderRows(g, source.data(), xOffset, yOffset, y, std::min(8, g.height - y), 0xFFFF, actual.data() + y * g.width);
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
  eye::LineReader reader;
  for (char c : std::string("Y -60\r")) assert(reader.push(c) == eye::LineReader::Pending);
  assert(reader.push('\n') == eye::LineReader::Ready && std::strcmp(reader.text, "Y -60") == 0);
  for (int i = 0; i < 100; ++i) reader.push('9');
  assert(reader.push('\n') == eye::LineReader::Invalid);
  reader.push('Y'); reader.push(' '); reader.push('1'); reader.push('\0');
  assert(reader.push('\n') == eye::LineReader::Invalid);
  for (char c : std::string("INFO")) reader.push(c);
  assert(reader.push('\n') == eye::LineReader::Ready && std::strcmp(reader.text, "INFO") == 0);

  // Odd dimensions, partial final stripe, exposed background, and exact RGB565 byte order.
  const eye::Geometry small{3, 5, 11, 10, 6, 3};
  std::vector<uint8_t> pattern(6 * 3 * 2);
  for (size_t i = 0; i < pattern.size(); ++i) pattern[i] = uint8_t(i * 13);
  assert(small.minOffset() == -2 && small.maxOffset() == 3);
  assert(small.minX() == -4 && small.maxX() == 3);
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
  assert(g.textureWidth <= g.canvasWidth && g.textureHeight <= g.canvasHeight);
  assert(g.minOffset() == -197 && g.maxOffset() == 198);
  assert(g.minX() == -213 && g.maxX() == 213);
  auto center = verify(g, image, 0);
  auto up = verify(g, image, -60);
  auto down = verify(g, image, 60);
  auto left = verify(g, image, 0, -60);
  auto right = verify(g, image, 0, 60);
  assert(center != up && center != down && up != down);
  assert(center != left && center != right && left != right);
  // Motion changes columns only, preserving every source row and the artwork orientation.
  for (int row = 0; row < g.height; ++row)
    for (int x = 0; x < g.width - 60; ++x) {
      assert(up[row * g.width + x] == center[row * g.width + x + 60]);
      assert(down[row * g.width + x + 60] == center[row * g.width + x]);
    }
  for (int row = 0; row < g.height - 60; ++row)
    for (int x = 0; x < g.width; ++x) {
      assert(right[row * g.width + x] == center[(row + 60) * g.width + x]);
      assert(left[(row + 60) * g.width + x] == center[row * g.width + x]);
    }
  for (int offset : {g.minOffset(), 0, g.maxOffset(), -1000, 1000})
    for (int horizontalOffset : {g.minX(), 0, g.maxX(), -1000, 1000}) verify(g, image, offset, horizontalOffset);
  assert(verify(g, image, 0) == center);
  writeFrame(".build/center.ppm", g, center);
  writeFrame(".build/up.ppm", g, up);
  writeFrame(".build/down.ppm", g, down);
  writeFrame(".build/left.ppm", g, left);
  writeFrame(".build/right.ppm", g, right);
  std::cout << "PASS actual artwork: both axes for sideways mounting, fixed crop, bounds, stripes, RGB565; XY/legacy serial parser and recovery\n";
}
