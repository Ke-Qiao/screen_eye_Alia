#pragma once

// Portrait mounting: eye 1 connector at top, eye 2 connector at bottom.
#define EYE1_FLIPPED false
#define EYE2_FLIPPED true
#define TFT_WIDTH 240
#define TFT_HEIGHT 320
#define EYE1_MOSI 23
#define EYE1_SCLK 18
#define EYE1_CS 5
#define EYE1_DC 27
#define EYE1_BL 25
#define EYE2_MOSI 13
#define EYE2_SCLK 14
#define EYE2_CS 22
#define EYE2_DC 32
#define EYE2_BL 26
// Neither panel has RST/RES or MISO connected; use software reset and write-only SPI.
#define EYE_BACKLIGHT_PERCENT 8
// Diagnostic clock reduction: 40 MHz showed intermittent color/stripe corruption.
#define SPI_FREQUENCY 10000000

#define EYE_CANVAS_WIDTH 720
#define EYE_CANVAS_HEIGHT 640
#define EYE_BACKGROUND 0x0000  // White on these panels in their original, accepted color mode.
#define EYE_SPEED 40.0f       // Nominal px/s; the 100 ms time-step cap limits each update to 4 px.
