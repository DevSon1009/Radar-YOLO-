#include <Arduino.h>
#include "config.h"

#define PIN_BUZ_TEST 16

void beep(int onLevel, int ms) {
  digitalWrite(PIN_BUZ_TEST, onLevel);
  delay(ms);
  digitalWrite(PIN_BUZ_TEST, !onLevel);
  delay(500);
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  pinMode(PIN_BUZ_TEST, OUTPUT);
  digitalWrite(PIN_BUZ_TEST, HIGH);   // trạng thái nghỉ (nếu module kích mức thấp)
}

void loop() {
  Serial.println("Thu kich muc THAP (LOW = keu)");
  digitalWrite(PIN_BUZ_TEST, HIGH);
  delay(1000);
  beep(LOW, 300);
  beep(LOW, 300);

  Serial.println("Thu kich muc CAO (HIGH = keu)");
  digitalWrite(PIN_BUZ_TEST, LOW);
  delay(1000);
  beep(HIGH, 300);
  beep(HIGH, 300);
}