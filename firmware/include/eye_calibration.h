#pragma once
#include <cstdint>
#include <cerrno>
#include <cstdlib>
#include <cstring>

namespace eye {
struct Calibration {
  int32_t centerX, centerY, angle10, flipped, reserved;
};

struct CalibrationStore {
  uint32_t magic;
  Calibration eyes[2];
};
constexpr uint32_t calibrationMagic = 0x43414C31;

inline unsigned backlightDuty(unsigned percent) {
  return (1024 * percent + 50) / 100;
}

inline bool parseBacklight(const char* text, int& result) {
  if (std::strncmp(text, "BL ", 3) != 0) return false;
  char* end;
  errno = 0;
  const long value = std::strtol(text + 3, &end, 10);
  if (end == text + 3 || *end != '\0' || errno == ERANGE || value < 0 || value > 100) return false;
  result = int(value);
  return true;
}

inline bool validCalibration(const Calibration& c, int width, int height) {
  return c.centerX >= 0 && c.centerX < width && c.centerY >= 0 && c.centerY < height &&
         c.angle10 >= -1800 && c.angle10 <= 1800 &&
         (c.flipped == 0 || c.flipped == 1) && (c.reserved == 0 || c.reserved == 1);  // Accept legacy NVS.
}

inline bool parseCalibration(const char* text, Calibration& result, int width, int height) {
  if (std::strncmp(text, "CAL ", 4) != 0) return false;
  const char* next = text + 4;
  int32_t values[5];
  for (int i = 0; i < 5; ++i) {
    char* end;
    errno = 0;
    const long value = std::strtol(next, &end, 10);
    if (end == next || errno == ERANGE || value < INT32_MIN || value > INT32_MAX ||
        (i < 4 ? *end != ' ' : *end != '\0')) return false;
    values[i] = int32_t(value);
    next = end + (i < 4 ? 1 : 0);
  }
  const Calibration c{values[0], values[1], values[2], values[3], values[4]};
  if (!validCalibration(c, width, height) || c.reserved != 0) return false;
  result = c;
  return true;
}
}  // namespace eye
