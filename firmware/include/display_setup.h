#pragma once

// ESP32 DevKit + ST7789S, portrait 240 x 320; SPI pins from the supplied diagram.
#define ST7789_DRIVER
#define TFT_WIDTH 240
#define TFT_HEIGHT 320
#define TFT_MOSI 23
#define TFT_SCLK 18
#define TFT_MISO 19
#define TFT_CS 5
#define TFT_DC 27
#define TFT_RST -1  // This module has no RST/RES pin; use the driver's software reset.
// Leave TFT_BL undefined so TFT_eSPI cannot turn the backlight fully on during init.
#define EYE_BACKLIGHT_PIN 25
#define EYE_BACKLIGHT_PERCENT 8
#define TFT_RGB_ORDER TFT_RGB
#define TFT_INVERSION_ON
#define SPI_FREQUENCY 40000000

#define EYE_CANVAS_WIDTH 720
#define EYE_CANVAS_HEIGHT 640
#define EYE_BACKGROUND 0xFFFF  // Match the supplied image's white background.
#define EYE_SPEED 160.0f      // Pixels per second; tune for the physical eye.
