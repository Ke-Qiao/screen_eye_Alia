#pragma once
#include <cerrno>
#include <cstring>
#include <sys/socket.h>

namespace eye {
inline bool peerClosed(int socketFd) {
  if (socketFd < 0) return true;
  char byte;
  const int count = recv(socketFd, &byte, 1, MSG_PEEK | MSG_DONTWAIT);
  return count == 0 || (count < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR);
}

struct NetworkConfig {
  char ssid[33] = {};
  char password[65] = {};
  char token[65] = {};
};

inline int hexDigit(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return -1;
}

inline bool validToken(const char* token) {
  if (std::strlen(token) != 64) return false;
  for (unsigned i = 0; i < 64; ++i) if (hexDigit(token[i]) < 0) return false;
  return true;
}

inline bool decodeField(const char*& cursor, char* out, unsigned capacity) {
  unsigned length = 0;
  while (*cursor && *cursor != ' ') {
    const int high = hexDigit(cursor[0]);
    const int low = cursor[1] ? hexDigit(cursor[1]) : -1;
    if (high < 0 || low < 0 || length >= capacity - 1 || (high == 0 && low == 0)) return false;
    out[length++] = char((high << 4) | low);
    cursor += 2;
  }
  out[length] = '\0';
  if (*cursor != ' ' || length == 0) return false;
  ++cursor;
  return true;
}

inline bool validConfig(const NetworkConfig& config) {
  if (!std::memchr(config.ssid, '\0', sizeof(config.ssid)) ||
      !std::memchr(config.password, '\0', sizeof(config.password)) ||
      !std::memchr(config.token, '\0', sizeof(config.token))) return false;
  const unsigned ssidSize = std::strlen(config.ssid), passwordSize = std::strlen(config.password);
  if (!ssidSize || ssidSize > 32 || !validToken(config.token)) return false;
  if (passwordSize >= 8 && passwordSize <= 63) return true;
  if (passwordSize != 64) return false;
  for (unsigned i = 0; i < 64; ++i) {
    const char c = config.password[i];
    if (hexDigit(c) < 0 && !(c >= 'A' && c <= 'F')) return false;
  }
  return true;
}

inline bool parseConfig(const char* command, NetworkConfig& out) {
  if (std::strncmp(command, "CONFIG ", 7) != 0) return false;
  NetworkConfig parsed;
  const char* cursor = command + 7;
  if (!decodeField(cursor, parsed.ssid, sizeof(parsed.ssid)) ||
      !decodeField(cursor, parsed.password, sizeof(parsed.password)) || !validToken(cursor)) return false;
  std::memcpy(parsed.token, cursor, 65);
  if (!validConfig(parsed)) return false;
  out = parsed;
  return true;
}

inline bool authorized(const char* command, const char* token) {
  if (std::strncmp(command, "AUTH ", 5) != 0 || !validToken(command + 5) || !validToken(token)) return false;
  unsigned mismatch = 0;
  for (unsigned i = 0; i < 64; ++i) mismatch |= unsigned(command[i + 5] ^ token[i]);
  return mismatch == 0;
}
}  // namespace eye
