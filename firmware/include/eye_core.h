#pragma once
#include <cerrno>
#include <cstdint>
#include <cstdlib>

namespace eye {
struct Geometry {
  int width, height, canvasWidth, canvasHeight, textureWidth, textureHeight;
  int cropX() const { return (canvasWidth - width) / 2; }
  int cropY() const { return (canvasHeight - height) / 2; }
  int textureX() const { return (canvasWidth - textureWidth) / 2; }
  int textureY() const { return (canvasHeight - textureHeight) / 2; }
  int minOffset() const { return -textureX(); }
  int maxOffset() const { return canvasWidth - textureWidth - textureX(); }
  int clamp(int offset) const { return offset < minOffset() ? minOffset() : (offset > maxOffset() ? maxOffset() : offset); }
  int minX() const { return -(canvasHeight - textureHeight - textureY()); }
  int maxX() const { return textureY(); }
  int clampX(int x) const { return x < minX() ? minX() : (x > maxX() ? maxX() : x); }
};

// A virtual canvas is a coordinate space, not a 900 KB RAM allocation.
// Decode only the requested output rows; all RGB565 source bytes stay in flash.
inline void renderRows(const Geometry& g, const uint8_t* texture, int xOffset, int yOffset,
                       int firstRow, int rows, uint16_t background, uint16_t* out) {
  xOffset = g.clampX(xOffset);
  yOffset = g.clamp(yOffset);
  // Sideways panel: viewer right = panel -Y, viewer down = panel +X.
  for (int row = 0; row < rows; ++row) {
    const int sy = g.cropY() + firstRow + row - g.textureY() + xOffset;
    for (int x = 0; x < g.width; ++x) {
      const int sx = g.cropX() + x - g.textureX() - yOffset;
      uint16_t color = background;
      if (sx >= 0 && sx < g.textureWidth && sy >= 0 && sy < g.textureHeight) {
        const int index = (sy * g.textureWidth + sx) * 2;
        color = uint16_t(texture[index]) | (uint16_t(texture[index + 1]) << 8);
      }
      *out++ = color;
    }
  }
}

inline bool parseOffset(const char* command, int& value) {
  if (command[0] != 'Y' || command[1] != ' ') return false;
  char* end;
  errno = 0;
  const long number = std::strtol(command + 2, &end, 10);
  if (end == command + 2 || *end != '\0' || errno == ERANGE || number < INT32_MIN || number > INT32_MAX) return false;
  value = int(number);
  return true;
}

inline bool parsePosition(const char* command, int& x, int& y) {
  if (command[0] != 'X' || command[1] != 'Y' || command[2] != ' ') return false;
  const char* start = command + 3;
  char* end;
  errno = 0;
  const long horizontal = std::strtol(start, &end, 10);
  if (end == start || *end != ' ' || errno == ERANGE || horizontal < INT32_MIN || horizontal > INT32_MAX) return false;
  start = end + 1;
  errno = 0;
  const long vertical = std::strtol(start, &end, 10);
  if (end == start || *end != '\0' || errno == ERANGE || vertical < INT32_MIN || vertical > INT32_MAX) return false;
  x = int(horizontal);
  y = int(vertical);
  return true;
}

// Newline framing survives split packets, CRLF, overlong input and embedded NULs.
template<unsigned Capacity>
struct LineBuffer {
  enum Result { Pending, Ready, Invalid };
  char text[Capacity] = {};
  unsigned length = 0;
  bool dropping = false;

  Result push(char c) {
    if (c == '\n') {
      if (length && text[length - 1] == '\r') --length;
      text[length] = '\0';
      const Result result = dropping ? Invalid : Ready;
      length = 0;
      dropping = false;
      return result;
    }
    if (static_cast<unsigned char>(c) < 32 && c != '\r') dropping = true;
    if (length == sizeof(text) - 1) dropping = true;
    if (!dropping) text[length++] = c;
    return Pending;
  }
};
using LineReader = LineBuffer<32>;
}  // namespace eye
