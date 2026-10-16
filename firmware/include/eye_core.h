#pragma once
#include <algorithm>
#include <cmath>
#include "eye_calibration.h"
#include <cerrno>
#include <cstdint>
#include <cstdlib>
#include <cstring>

namespace eye {
struct Geometry {
  int width, height, canvasWidth, canvasHeight, textureWidth, textureHeight;
  int cropX() const { return (canvasWidth - width) / 2; }
  int cropY() const { return (canvasHeight - height) / 2; }
  int textureX() const { return (canvasWidth - textureWidth) / 2; }
  int textureY() const { return (canvasHeight - textureHeight) / 2; }
  int minOffset() const { return -textureY(); }
  int maxOffset() const { return canvasHeight - textureHeight - textureY(); }
  int clamp(int offset) const { return offset < minOffset() ? minOffset() : (offset > maxOffset() ? maxOffset() : offset); }
  int minX() const { return -textureX(); }
  int maxX() const { return canvasWidth - textureWidth - textureX(); }
  int clampX(int x) const { return x < minX() ? minX() : (x > maxX() ? maxX() : x); }
};

// Inverse rotation is computed once per frame; pixels and stripe buffers stay small.
struct RenderTransform {
  float sourceX, sourceY, cosine, sine;
};

struct Rect {
  int left, top, right, bottom;
  int width() const { return right - left; }
  int height() const { return bottom - top; }
  bool empty() const { return width() <= 0 || height() <= 0; }
};

inline Rect textureBounds(const Geometry& g, const RenderTransform& t) {
  const float dx = t.sourceX - g.textureWidth / 2.0f, dy = t.sourceY - g.textureHeight / 2.0f;
  const float cx = 0.5f - t.cosine * dx + t.sine * dy;
  const float cy = 0.5f - t.sine * dx - t.cosine * dy;
  const float rx = (std::abs(t.cosine) * g.textureWidth + std::abs(t.sine) * g.textureHeight) / 2;
  const float ry = (std::abs(t.sine) * g.textureWidth + std::abs(t.cosine) * g.textureHeight) / 2;
  const auto clip = [](int value, int limit) { return value < 0 ? 0 : (value > limit ? limit : value); };
  // Include a margin for float stepping at rotated texture boundaries.
  return {clip(int(std::floor(cx - rx)) - 2, g.width), clip(int(std::floor(cy - ry)) - 2, g.height),
          clip(int(std::ceil(cx + rx)) + 2, g.width), clip(int(std::ceil(cy + ry)) + 2, g.height)};
}

inline Rect unite(Rect a, Rect b) {
  if (a.empty()) return b;
  if (b.empty()) return a;
  return {a.left < b.left ? a.left : b.left, a.top < b.top ? a.top : b.top,
          a.right > b.right ? a.right : b.right, a.bottom > b.bottom ? a.bottom : b.bottom};
}

inline RenderTransform makeTransform(const Geometry& g, int x, int y, const Calibration& c) {
  const float radians = c.angle10 * (3.14159265358979323846f / 1800.0f);
  const float cosine = std::cos(radians), sine = std::sin(radians);
  const float centerX = g.textureX() + g.clampX(x) + c.centerX - g.width / 2 - g.cropX() + g.textureWidth / 2.0f;
  const float centerY = g.textureY() + g.clamp(y) + c.centerY - g.height / 2 - g.cropY() + g.textureHeight / 2.0f;
  return {g.textureWidth / 2.0f + cosine * (0.5f - centerX) + sine * (0.5f - centerY),
          g.textureHeight / 2.0f - sine * (0.5f - centerX) + cosine * (0.5f - centerY), cosine, sine};
}

inline void renderRows(const Geometry& g, const uint8_t* texture, const RenderTransform& t,
                       int firstRow, int rows, uint16_t background, uint16_t* out) {
#if __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__
  if (t.cosine == 1.0f && t.sine == 0.0f) {
    const int left = int(std::floor(t.sourceX)), top = int(std::floor(t.sourceY));
    const int first = std::max(0, -left), last = std::min(g.width, g.textureWidth - left);
    for (int row = firstRow; row < firstRow + rows; ++row, out += g.width) {
      std::fill(out, out + g.width, background);
      const int y = top + row;
      if (y >= 0 && y < g.textureHeight && first < last)
        std::memcpy(out + first, texture + 2 * (y * g.textureWidth + first + left), 2 * (last - first));
    }
    return;
  }
#endif
  for (int row = firstRow; row < firstRow + rows; ++row) {
    float sx = t.sourceX + row * t.sine, sy = t.sourceY + row * t.cosine;
    for (int x = 0; x < g.width; ++x, sx += t.cosine, sy -= t.sine) {
      uint16_t color = background;
      if (sx >= 0 && sx < g.textureWidth && sy >= 0 && sy < g.textureHeight) {
        const int index = (int(sy) * g.textureWidth + int(sx)) * 2;
        color = uint16_t(texture[index]) | (uint16_t(texture[index + 1]) << 8);
      }
      *out++ = color;
    }
  }
}

inline void renderRegionRows(const Geometry& g, const uint8_t* texture, const RenderTransform& t,
                             Rect region, int firstRow, int rows, uint16_t background, uint16_t* out) {
  renderRows(g, texture, t, firstRow, rows, background, out);
  // Pack the existing pixels without changing their sampling or RGB565 values.
  for (int row = 0; row < rows; ++row)
    std::memmove(out + row * region.width(), out + row * g.width + region.left, region.width() * sizeof(uint16_t));
}

inline void renderRows(const Geometry& g, const uint8_t* texture, int xOffset, int yOffset,
                       int firstRow, int rows, uint16_t background, uint16_t* out) {
  const Calibration centered{g.width / 2, g.height / 2, 0, 0, 0};
  renderRows(g, texture, makeTransform(g, xOffset, yOffset, centered), firstRow, rows, background, out);
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

// Selection is part of each command, never shared mutable connection state.
inline bool parseEyeCommand(const char* text, int& index, const char*& command) {
  if (std::strncmp(text, "EYE ", 4) != 0 || (text[4] != '1' && text[4] != '2') || text[5] != ' ')
    return false;
  index = text[4] - '1';
  command = text + 6;
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
