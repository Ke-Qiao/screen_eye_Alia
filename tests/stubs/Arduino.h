#pragma once
#include <cassert>
#include <cstdint>
#include <vector>

constexpr int OUTPUT = 1, LOW = 0, HIGH = 1;
namespace mock {
extern int levels[40];
extern std::vector<unsigned> delays;
}
inline void pinMode(int pin, int mode) { assert(pin >= 0 && pin < 40 && mode == OUTPUT); }
inline void digitalWrite(int pin, int value) { mock::levels[pin] = value; }
inline void delay(unsigned milliseconds) { mock::delays.push_back(milliseconds); }
