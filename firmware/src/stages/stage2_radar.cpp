// Radar + relay den + coi + Wi-Fi gui telemetry JSON
#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <time.h>
#include <ld2410.h>
#include "config.h"
#include "secrets.h"

#ifndef PIN_RELAY
#define PIN_RELAY 4
#endif
#define PIN_BUZZER 5
#define PIN_ACK    0

#define RELAY_ON_LEVEL  LOW    // DAT DUNG gia tri dang chay dung cua ban
#define BUZZER_ON_LEVEL LOW    // DAT DUNG gia tri dang chay dung cua ban
#define ENERGY_MIN  40
#define CONFIRM_MS  2000
#define CLEAR_MS    3000
#define BEEP_MS     300
#define SEND_MS     2000       // gui telemetry dinh ky 2 giay

enum State { DISARMED, ARMED, DETECTED, ALARMING, ACKNOWLEDGED };
const char* STATE_NAME[] = {"DISARMED", "ARMED", "DETECTED", "ALARMING", "ACKNOWLEDGED"};

ld2410 radar;
volatile State state = ARMED;
volatile uint32_t stateVersion = 0;   // tang moi lan doi trang thai
volatile bool g_presence = false;
volatile int  g_signal = 0;
uint32_t tState = 0, lastSeen = 0, lastPrint = 0, lastBeep = 0;
bool beepOn = false;

void relaySet(bool on)  { digitalWrite(PIN_RELAY,  on ? RELAY_ON_LEVEL  : !RELAY_ON_LEVEL); }
void buzzerSet(bool on) { digitalWrite(PIN_BUZZER, on ? BUZZER_ON_LEVEL : !BUZZER_ON_LEVEL); }

void setState(State s) {
  state = s;
  stateVersion = stateVersion + 1;
  tState = millis();
  Serial.printf(">>> %s\n", STATE_NAME[s]);
  relaySet(s == ALARMING);
  beepOn = (s == ALARMING);
  buzzerSet(beepOn);
  lastBeep = millis();
}

// ---- Task mang: chay rieng tren core 0, khong chan radar ----
void netTask(void*) {
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  bool wasConnected = false, timeOk = false;
  uint32_t lastSend = 0, lastWarn = 0;
  uint32_t seenVersion = stateVersion;

  for (;;) {
    if (WiFi.status() != WL_CONNECTED) {
      wasConnected = false;
      if (millis() - lastWarn > 5000) { lastWarn = millis(); Serial.println("WiFi: dang ket noi..."); }
      vTaskDelay(pdMS_TO_TICKS(500));
      continue;
    }
    if (!wasConnected) {
      wasConnected = true;
      Serial.printf("WiFi OK | IP %s | RSSI %d dBm\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
      configTime(0, 0, "pool.ntp.org", "time.google.com");
    }
    if (time(nullptr) < 1700000000) {
      if (millis() - lastWarn > 5000) { lastWarn = millis(); Serial.println("Dang dong bo gio NTP..."); }
      vTaskDelay(pdMS_TO_TICKS(300));
      continue;
    }
    if (!timeOk) { timeOk = true; Serial.println("NTP OK"); }

    bool changed = (stateVersion != seenVersion);
    if (changed || millis() - lastSend >= SEND_MS) {
      seenVersion = stateVersion;
      lastSend = millis();

      time_t now = time(nullptr);
      struct tm t;
      gmtime_r(&now, &t);
      char ts[25];
      strftime(ts, sizeof ts, "%Y-%m-%dT%H:%M:%SZ", &t);

      State s = state;
      char body[300];
      snprintf(body, sizeof body,
        "{\"device_id\":\"%s\",\"source\":\"radar\",\"event_type\":\"%s\","
        "\"presence\":%s,\"signal\":%d,\"armed\":%s,\"state\":\"%s\",\"timestamp\":\"%s\"}",
        DEVICE_ID, changed ? "state_change" : "telemetry",
        g_presence ? "true" : "false", (int)g_signal,
        (s != DISARMED) ? "true" : "false", STATE_NAME[s], ts);

      HTTPClient http;
      http.setConnectTimeout(1000);
      http.setTimeout(1500);
      http.begin(SERVER_URL);
      http.addHeader("Content-Type", "application/json");
      int code = http.POST(body);
      http.end();
      Serial.printf("[GUI] %s -> HTTP %d\n", body, code);
    }
    vTaskDelay(pdMS_TO_TICKS(100));
  }
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  pinMode(PIN_RELAY, OUTPUT);
  pinMode(PIN_BUZZER, OUTPUT);
  pinMode(PIN_ACK, INPUT_PULLUP);
  relaySet(false);
  buzzerSet(false);

  relaySet(true); buzzerSet(true); delay(500);
  relaySet(false); buzzerSet(false);

  pinMode(PIN_RADAR_OUT, INPUT);
  Serial1.begin(RADAR_BAUD, SERIAL_8N1, PIN_RADAR_RX, PIN_RADAR_TX);
  if (radar.begin(Serial1)) Serial.println("Radar LD2410C: KET NOI OK");
  else Serial.println("Radar LD2410C: KHONG THAY");
  Serial.println("Lenh: a = ARMED | d = DISARMED | k = xac nhan (hoac nhan nut BOOT)");
  setState(ARMED);

  xTaskCreatePinnedToCore(netTask, "net", 10240, NULL, 1, NULL, 0);
}

void loop() {
  radar.read();

  int sig = max((int)radar.movingTargetEnergy(), (int)radar.stationaryTargetEnergy());
  if (sig > 100) sig = 100;
  bool present = radar.isConnected() && radar.presenceDetected() && sig >= ENERGY_MIN;
  g_presence = present;
  g_signal = sig;

  if (present) lastSeen = millis();
  bool gone = millis() - lastSeen > CLEAR_MS;

  while (Serial.available()) {
    char c = Serial.read();
    if (c == 'a') setState(ARMED);
    else if (c == 'd') setState(DISARMED);
    else if (c == 'k' && state == ALARMING) setState(ACKNOWLEDGED);
  }

  static bool lastBtn = HIGH;
  bool btn = digitalRead(PIN_ACK);
  if (lastBtn == HIGH && btn == LOW && state == ALARMING) setState(ACKNOWLEDGED);
  lastBtn = btn;

  switch (state) {
    case DISARMED:
      break;
    case ARMED:
      if (present) setState(DETECTED);
      break;
    case DETECTED:
      if (millis() - lastSeen > 1000) setState(ARMED);
      else if (millis() - tState >= CONFIRM_MS) setState(ALARMING);
      break;
    case ALARMING:
      if (gone) { setState(ARMED); break; }
      if (millis() - lastBeep >= BEEP_MS) {
        lastBeep = millis();
        beepOn = !beepOn;
        buzzerSet(beepOn);
      }
      break;
    case ACKNOWLEDGED:
      if (gone) setState(ARMED);
      break;
  }

  if (millis() - lastPrint >= 2000) {
    lastPrint = millis();
    Serial.printf("%-12s | %s | nang luong %3d\n",
                  STATE_NAME[state], present ? "CO NGUOI" : "khong co", sig);
  }
}