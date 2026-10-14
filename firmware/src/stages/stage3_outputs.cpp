// Buoc 3: thu relay + den 12V + coi (KHONG can radar/WiFi)
#include <Arduino.h>
#include "config.h"

#ifndef PIN_BUZZER
#define PIN_BUZZER 5
#endif
#define BUZZER_ON_LEVEL LOW   // coi keu nguoc thi doi thanh HIGH

static void setRelay(bool on)  { digitalWrite(PIN_RELAY,  (on != (bool)RELAY_ACTIVE_LOW) ? HIGH : LOW); }
static void setBuzzer(bool on) { digitalWrite(PIN_BUZZER, on ? BUZZER_ON_LEVEL : !BUZZER_ON_LEVEL); }

void setup() {
  Serial.begin(115200);
  pinMode(PIN_RELAY, OUTPUT);
  pinMode(PIN_BUZZER, OUTPUT);
  setRelay(false);
  setBuzzer(false);
  delay(1000);
}

void loop() {
  Serial.println("Relay BAT (den 12V phai sang) 2 giay");
  setRelay(true);  delay(2000);
  setRelay(false); delay(1000);

  Serial.println("Coi keu 3 tieng");
  for (int i = 0; i < 3; i++) {
    setBuzzer(true);  delay(300);
    setBuzzer(false); delay(300);
  }
  Serial.println("--- nghi 3 giay ---");
  delay(3000);
}