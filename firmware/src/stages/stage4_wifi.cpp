// Bước 4: nối WiFi và gọi thử GET /health của backend
#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include "secrets.h"

void setup() {
  Serial.begin(115200);
  delay(1000);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("Dang noi WiFi");
  while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print("."); }
  Serial.printf("\nDa noi WiFi. IP cua ESP32: %s\n", WiFi.localIP().toString().c_str());
}

void loop() {
  HTTPClient http;
  http.setConnectTimeout(3000);
  http.setTimeout(3000);
  String url = String(API_BASE) + "/health";
  if (http.begin(url)) {
    int code = http.GET();
    Serial.printf("GET %s -> %d  %s\n", url.c_str(), code, code > 0 ? http.getString().c_str() : http.errorToString(code).c_str());
    http.end();
  }
  delay(3000);
}
