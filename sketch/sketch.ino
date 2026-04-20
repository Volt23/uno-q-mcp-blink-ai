// SPDX-License-Identifier: MPL-2.0
//
// UNO Q Remote MCP Starter — MCU-side providers.
//
// Bridge RPC surface (called from the Linux/MPU Python side):
//   - set_led3_color(int r, int g, int b)   PWM, values 0..255
//   - set_led4_color(bool r, bool g, bool b) digital, active-low
//   - matrix_draw(std::vector<uint8_t>)     96 bytes, one per LED, 0..7 brightness

#include <Arduino_RouterBridge.h>
#include <Arduino_LED_Matrix.h>
#include <vector>
#include <zephyr/kernel.h>

Arduino_LED_Matrix matrix;

// The Router runs bridge providers on a separate thread from loop().
// This mutex serializes LED matrix writes so a concurrent RPC can't tear a frame.
K_MUTEX_DEFINE(matrix_mtx);

void set_led3_color(int r, int g, int b) {
  analogWrite(LED3_R, constrain(r, 0, 255));
  analogWrite(LED3_G, constrain(g, 0, 255));
  analogWrite(LED3_B, constrain(b, 0, 255));
}

void set_led4_color(bool r, bool g, bool b) {
  digitalWrite(LED4_R, r ? LOW : HIGH);
  digitalWrite(LED4_G, g ? LOW : HIGH);
  digitalWrite(LED4_B, b ? LOW : HIGH);
}

void matrix_draw(std::vector<uint8_t> frame) {
  if (frame.empty()) return;
  k_mutex_lock(&matrix_mtx, K_FOREVER);
  matrix.draw(frame.data());
  k_mutex_unlock(&matrix_mtx);
}

void setup() {
  pinMode(LED4_R, OUTPUT);
  pinMode(LED4_G, OUTPUT);
  pinMode(LED4_B, OUTPUT);
  set_led3_color(0, 0, 0);
  set_led4_color(false, false, false);

  matrix.begin();
  // 3 bits per pixel → accept brightness values 0..7 from the backend.
  matrix.setGrayscaleBits(3);
  matrix.clear();

  Bridge.begin();
  Bridge.provide("set_led3_color", set_led3_color);
  Bridge.provide("set_led4_color", set_led4_color);
  Bridge.provide("matrix_draw", matrix_draw);
}

void loop() {
  delay(5);
}
