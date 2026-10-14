// ESP32-S3 + LD2410C + backend + relay/den + buzzer.
#include <Arduino.h>
#include <WiFi.h>
#include <WiFiProv.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <esp_wifi.h>
#include <ld2410.h>
#include "config.h"
#include "secrets.h"

enum DeviceState : uint8_t { DISARMED, ARMED, DETECTED, ALARMING, ACKNOWLEDGED };

static ld2410 radar;
static volatile bool gRadarOk = false;
static volatile bool gPresence = false;
static volatile uint8_t gMoving = 0;
static volatile uint8_t gStationary = 0;
static volatile uint16_t gMovingDistanceCm = 0;
static volatile uint16_t gStationaryDistanceCm = 0;
static volatile bool gArmed = true;
static volatile bool gSimulationEnabled = SIMULATE_PRESENCE_BY_DEFAULT;
static volatile bool gLightOn = false;
static volatile bool gBuzzerOn = false;
static volatile bool gBuzzerOverride = false;
static volatile DeviceState gState = ARMED;
static volatile bool gProvisionRequested = false;
static volatile bool gProvisioningActive = false;
static volatile bool gProvisioningStarted = false;
static volatile bool gHaveSavedWifiCredentials = false;
static uint32_t gProvisionStartRequestedAt = 0;
static uint32_t gDetectedSince = 0;
static uint32_t gLastPresence = 0;
static uint32_t gAlarmSince = 0;

static void wifiEventHandler(arduino_event_t* event) {
  switch (event->event_id) {
    case ARDUINO_EVENT_WIFI_STA_GOT_IP:
      Serial.printf("[wifi] da ket noi, IP %s, RSSI %d dBm\n",
                    WiFi.localIP().toString().c_str(), WiFi.RSSI());
      break;
    case ARDUINO_EVENT_WIFI_STA_DISCONNECTED:
      Serial.println("[wifi] mat ket noi; dang thu ket noi lai");
      break;
    case ARDUINO_EVENT_PROV_START:
      gProvisioningActive = true;
      gProvisioningStarted = true;
      Serial.printf("[prov] BLE san sang; chon %s trong ESP Provisioning\n",
                    WIFI_PROV_SERVICE_NAME);
      break;
    case ARDUINO_EVENT_PROV_CRED_RECV:
      Serial.println("[prov] da nhan thong tin Wi-Fi; dang thu ket noi");
      break;
    case ARDUINO_EVENT_PROV_CRED_FAIL:
      if (event->event_info.prov_fail_reason == WIFI_PROV_STA_AUTH_ERROR) {
        Serial.println("[prov] ket noi that bai: mat khau Wi-Fi khong dung");
      } else if (event->event_info.prov_fail_reason == WIFI_PROV_STA_AP_NOT_FOUND) {
        Serial.println("[prov] ket noi that bai: khong tim thay Wi-Fi (kiem tra mang 2.4 GHz)");
      } else {
        Serial.println("[prov] ket noi Wi-Fi that bai; hay thu lai tu ung dung");
      }
      break;
    case ARDUINO_EVENT_PROV_CRED_SUCCESS:
      gHaveSavedWifiCredentials = true;
      Serial.println("[prov] Wi-Fi moi da ket noi; thong tin duoc luu tren thiet bi");
      break;
    case ARDUINO_EVENT_PROV_END:
      gProvisioningActive = false;
      gProvisioningStarted = false;
      Serial.println("[prov] phien BLE provisioning da ket thuc");
      break;
    default:
      break;
  }
}

static bool readSavedWifiCredentials(bool& hasCredentials) {
  wifi_config_t config = {};
  if (esp_wifi_get_config(WIFI_IF_STA, &config) != ESP_OK) {
    Serial.println("[wifi] khong doc duoc cau hinh Wi-Fi da luu");
    return false;
  }
  hasCredentials = config.sta.ssid[0] != '\0';
  return true;
}

static void startBleProvisioning(bool replaceSavedCredentials) {
  if (gProvisioningActive) return;

  if (replaceSavedCredentials) {
    WiFi.disconnect(false, false);
    gHaveSavedWifiCredentials = false;
  }

  gProvisioningActive = true;
  gProvisioningStarted = false;
  gProvisionStartRequestedAt = millis();
  Serial.println(replaceSavedCredentials
                     ? "[prov] bat dau doi Wi-Fi theo yeu cau nut nhan"
                     : "[prov] chua co Wi-Fi da luu; bat dau provisioning BLE");
  WiFiProv.beginProvision(
      WIFI_PROV_SCHEME_BLE,
      WIFI_PROV_SCHEME_HANDLER_FREE_BTDM,
      WIFI_PROV_SECURITY_1,
      BLE_PROV_POP,
      WIFI_PROV_SERVICE_NAME,
      nullptr,
      nullptr,
      replaceSavedCredentials);
}

static inline void setRelay(bool on) {
  const bool activeLow = RELAY_ACTIVE_LOW != 0;
  digitalWrite(PIN_RELAY, on == activeLow ? LOW : HIGH);
}

static inline void setBuzzer(bool on) {
  const bool activeLow = BUZZER_ACTIVE_LOW != 0;
  digitalWrite(PIN_BUZZER, on == activeLow ? LOW : HIGH);
}

static bool alarmPulseActive() {
  return gState == ALARMING && !gBuzzerOverride && (millis() / 400) % 2 == 0;
}

static void updateOutputs() {
  const bool alarmPulse = alarmPulseActive();
  setRelay(gLightOn || alarmPulse);
  setBuzzer(gBuzzerOn || alarmPulse);
}

static int httpRequest(bool post, const String& path, const String& body, String& response) {
  HTTPClient http;
  const String url = String(API_BASE) + path;
  if (!http.begin(url)) {
    Serial.println("[http] URL backend khong hop le");
    return -1;
  }
  http.setConnectTimeout(HTTP_TIMEOUT_MS);
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.addHeader("X-Device-Key", DEVICE_KEY);
  int code;
  if (post) {
    http.addHeader("Content-Type", "application/json");
    code = http.POST(body);
  } else {
    code = http.GET();
  }
  if (code > 0) response = http.getString();
  http.end();
  return code;
}

static void sendTelemetry() {
  const bool radarOk = gRadarOk;
  const bool presence = gPresence;
  JsonDocument doc;
  doc["device_id"] = DEVICE_ID;
  doc["state"] = static_cast<const char*>(
      gState == DISARMED ? "DISARMED" :
      gState == DETECTED ? "DETECTED" :
      gState == ALARMING ? "ALARMING" :
      gState == ACKNOWLEDGED ? "ACKNOWLEDGED" : "ARMED");
  doc["moving_energy"] = gMoving;
  doc["stationary_energy"] = gStationary;
  doc["moving_distance_cm"] = gMovingDistanceCm;
  doc["stationary_distance_cm"] = gStationaryDistanceCm;
  doc["radar_ok"] = radarOk;
  doc["source"] = radarOk ? "radar" : (gSimulationEnabled ? "simulation" : "unavailable");
  const bool alarmPulse = alarmPulseActive();
  doc["light_on"] = gLightOn || alarmPulse;
  doc["buzzer_on"] = gBuzzerOn || alarmPulse;
  String body, response;
  serializeJson(doc, body);
  const int code = httpRequest(true, "/api/events", body, response);
  if (code != 200) Serial.printf("[telemetry] loi HTTP %d\n", code);
}

static void handleCommand(const char* command) {
  Serial.printf("[cmd] %s\n", command);
  if (!strcmp(command, "arm")) {
    gArmed = true;
  } else if (!strcmp(command, "disarm")) {
    gArmed = false;
  } else if (!strcmp(command, "ack")) {
    if (gState == ALARMING) gState = ACKNOWLEDGED;
  } else if (!strcmp(command, "light_on")) {
    gLightOn = true;
  } else if (!strcmp(command, "light_off")) {
    gLightOn = false;
  } else if (!strcmp(command, "buzzer_on")) {
    gBuzzerOn = true;
    gBuzzerOverride = true;
  } else if (!strcmp(command, "buzzer_off")) {
    gBuzzerOn = false;
    gBuzzerOverride = true;
  } else if (!strcmp(command, "simulate_on")) {
    gSimulationEnabled = true;
  } else if (!strcmp(command, "simulate_off")) {
    gSimulationEnabled = false;
  }
  updateOutputs();
}

static void pollCommands() {
  String response;
  const String path = String("/api/devices/") + DEVICE_ID + "/command";
  const int code = httpRequest(false, path, "", response);
  if (code != 200) {
    Serial.printf("[cmd] loi HTTP %d\n", code);
    return;
  }

  JsonDocument doc;
  const DeserializationError error = deserializeJson(doc, response);
  if (error) {
    Serial.printf("[cmd] JSON khong hop le: %s\n", error.c_str());
    return;
  }
  const char* command = doc["cmd"] | "";
  if (*command) handleCommand(command);
}

static void netTask(void*) {
  uint32_t lastTelemetry = 0;
  uint32_t lastCommandPoll = 0;
  uint32_t lastRetry = 0;
  const uint32_t startedAt = millis();
  bool autoProvisionStarted = false;
  bool reportedConnected = false;

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.persistent(true);
  bool hasSavedCredentials = false;
  const bool wifiConfigReadable = readSavedWifiCredentials(hasSavedCredentials);
  gHaveSavedWifiCredentials = hasSavedCredentials;
  if (hasSavedCredentials) {
    WiFi.begin();
  } else {
    WiFi.begin(WIFI_SSID, WIFI_PASS);
  }
  Serial.println(gHaveSavedWifiCredentials
                     ? "[wifi] dang thu Wi-Fi da luu"
                     : "[wifi] chua co Wi-Fi da luu; cho provisioning BLE");

  for (;;) {
    const uint32_t now = millis();
    if (gProvisionRequested) {
      gProvisionRequested = false;
      if (!gProvisioningActive) {
        autoProvisionStarted = true;
        startBleProvisioning(true);
      }
    }
    if (gProvisioningActive && !gProvisioningStarted &&
        now - gProvisionStartRequestedAt >= 10000) {
      gProvisioningActive = false;
      Serial.println("[prov] BLE khong khoi dong; kiem tra log loi va thu giu nut de bat lai");
    }
    if (gProvisioningActive) {
      vTaskDelay(pdMS_TO_TICKS(250));
      continue;
    }

    if (WiFi.status() != WL_CONNECTED) {
      reportedConnected = false;
      if (wifiConfigReadable && !gHaveSavedWifiCredentials && !autoProvisionStarted &&
          now - startedAt >= WIFI_PROV_AUTO_START_MS) {
        autoProvisionStarted = true;
        startBleProvisioning(false);
        vTaskDelay(pdMS_TO_TICKS(250));
        continue;
      }
      if (now - lastRetry >= WIFI_RETRY_MS) {
        Serial.println("[wifi] dang thu ket noi lai");
        WiFi.reconnect();
        lastRetry = millis();
      }
      vTaskDelay(pdMS_TO_TICKS(500));
      continue;
    }
    if (!reportedConnected) {
      reportedConnected = true;
      gHaveSavedWifiCredentials = true;
      Serial.printf("[wifi] san sang gui du lieu, IP %s, RSSI %d dBm\n",
                    WiFi.localIP().toString().c_str(), WiFi.RSSI());
    }
    if (now - lastTelemetry >= TELEMETRY_MS) {
      lastTelemetry = now;
      sendTelemetry();
    }
    if (now - lastCommandPoll >= POLL_MS) {
      lastCommandPoll = now;
      pollCommands();
    }
    vTaskDelay(pdMS_TO_TICKS(50));
  }
}

void setup() {
  Serial.begin(115200);
  delay(500);
  pinMode(PIN_RELAY, OUTPUT);
  pinMode(PIN_BUZZER, OUTPUT);
  pinMode(PIN_WIFI_PROV_BUTTON, INPUT_PULLUP);
  setRelay(false);
  setBuzzer(false);
  Serial.printf("\n=== Radar IoT firmware %s | %s ===\n", FW_VERSION, DEVICE_ID);
  WiFi.onEvent(wifiEventHandler);

  Serial1.begin(RADAR_BAUD, SERIAL_8N1, PIN_RADAR_RX, PIN_RADAR_TX);
  Serial.println(radar.begin(Serial1, false)
                     ? "[radar] LD2410C da phat hien"
                     : "[radar] khong thay LD2410C; se mo phong neu duoc bat");

  xTaskCreatePinnedToCore(netTask, "net", 10240, nullptr, 1, nullptr, 0);
}

void loop() {
  static uint32_t buttonPressedAt = 0;
  static bool buttonHandled = false;
  static bool buttonArmed = false;
  const uint32_t buttonNow = millis();
  if (digitalRead(PIN_WIFI_PROV_BUTTON) == HIGH) {
    buttonArmed = true;
    buttonPressedAt = 0;
    buttonHandled = false;
  } else if (buttonArmed) {
    if (buttonPressedAt == 0) buttonPressedAt = buttonNow;
    if (!buttonHandled &&
        buttonNow - buttonPressedAt >= WIFI_PROV_BUTTON_HOLD_MS) {
      buttonHandled = true;
      gProvisionRequested = true;
      Serial.println("[prov] nut giu 3 giay; se mo BLE provisioning");
    }
  }

  radar.read();
  const bool radarOk = radar.isConnected();
  gRadarOk = radarOk;

  bool presence = false;
  uint8_t moving = 0;
  uint8_t stationary = 0;
  uint16_t movingDistance = 0;
  uint16_t stationaryDistance = 0;
  if (radarOk) {
    presence = radar.presenceDetected();
    moving = radar.movingTargetEnergy();
    stationary = radar.stationaryTargetEnergy();
    movingDistance = radar.movingTargetDistance();
    stationaryDistance = radar.stationaryTargetDistance();
  } else if (gSimulationEnabled) {
    const uint32_t phase = (millis() + 15000) % 30000;
    presence = phase < 7000;
    if (presence) {
      moving = 72;
      stationary = 48;
    }
  }
  gPresence = presence;
  gMoving = moving;
  gStationary = stationary;
  gMovingDistanceCm = movingDistance;
  gStationaryDistanceCm = stationaryDistance;

  const uint32_t now = millis();
  if (presence) gLastPresence = now;

  if (!gArmed) {
    gState = DISARMED;
    gDetectedSince = 0;
  } else if (gState == DISARMED) {
    gState = ARMED;
  }

  if (gArmed) {
    if (presence && gState == ARMED) {
      gState = DETECTED;
      gDetectedSince = now;
    } else if (presence && gState == DETECTED &&
               now - gDetectedSince >= PRESENCE_CONFIRM_MS) {
      gState = ALARMING;
      gAlarmSince = now;
      gBuzzerOverride = false;
    } else if (!presence && gState == DETECTED) {
      gState = ARMED;
    } else if (!presence &&
               (gState == ALARMING || gState == ACKNOWLEDGED) &&
               now - gLastPresence >= PRESENCE_CLEAR_MS) {
      gState = ARMED;
    }
    if (gState == ALARMING && now - gAlarmSince >= ALARM_MAX_MS) {
      Serial.println("[alarm] het thoi gian toi da; tam dung bao dong");
      gState = ACKNOWLEDGED;
    }
  }

  updateOutputs();
  static uint32_t lastPrint = 0;
  if (now - lastPrint >= 2000) {
    lastPrint = now;
        Serial.printf("[status] radar=%s source=%s presence=%d state=%u signal=%u/%u btn=%d\n",
                  radarOk ? "OK" : "OFFLINE",
                  radarOk ? "that" : (gSimulationEnabled ? "mo phong" : "khong co"),
                  presence, static_cast<unsigned>(gState), moving, stationary,
                  digitalRead(PIN_WIFI_PROV_BUTTON));
  }
  delay(20);
}
