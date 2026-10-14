# BLE Wi-Fi provisioning

The production `firmware` environment uses the ESP32 Arduino Core 2.0.17
provided by PlatformIO Espressif32 7.0.1. BLE provisioning uses the bundled
Espressif `WiFiProv` library and its standard Security 1 protocol; no additional
PlatformIO library is required. The production build keeps Arduino logs at
ERROR level so the core library's INFO-level provisioning log cannot print the
proof-of-possession PIN; firmware status and error messages remain available.

## Provisioning behavior

- On boot, the device first connects with Wi-Fi credentials already stored in
  ESP32 NVS. If NVS has no credentials, it tries the local `WIFI_SSID` and
  `WIFI_PASS` fallback in `include/secrets.h`. Successful connection credentials
  are kept in NVS for later boots. The radar loop and outputs continue running
  while networking works.
- If there are no saved credentials and neither the saved nor fallback network
  connects within 30 seconds, BLE provisioning starts. A temporary failure to
  reconnect to a previously saved network does not erase those credentials or
  automatically start provisioning.
- To replace a saved network, first release the GPIO 6 button after boot, then
  hold it for 3 seconds. This deliberately clears the old Wi-Fi configuration
  and starts BLE provisioning. A button held continuously during boot is ignored
  until released, so it cannot block the normal Wi-Fi connection.
- The BLE service name is `PROV_RADAR01`. Security 1 requires the proof of
  possession (`BLE_PROV_POP`) defined in the local, ignored `include/secrets.h`.
- Wi-Fi credentials and provisioning PIN are never printed to the serial log.

## Button wiring

Use a momentary normally-open push button between GPIO 6 and GND. The firmware
enables the internal pull-up, so no external resistor is required. GPIO 6 is
not used by the production radar, relay, or buzzer configuration. Do not use
GPIO 26-37 on this ESP32-S3-N16R8 board because they are reserved for flash and
PSRAM.

## iPhone setup

1. Install/open Espressif's official **ESP Provisioning** app.
2. Power the ESP32. For first-time setup, wait up to 30 seconds for BLE
   provisioning to start. To change a saved network, hold the GPIO 6 button
   for 3 seconds instead.
3. In the app, tap **Provision Device**, choose the BLE transport, and select
   `PROV_RADAR01`.
4. Enter the proof-of-possession PIN from the local `BLE_PROV_POP` setting when
   requested. Choose the destination 2.4 GHz Wi-Fi network and enter its
   password in the app.
5. Wait for the app to report success. The device stores the credentials in
   NVS and reconnects to that network after future reboots.

## Verify or troubleshoot

- On success, the serial log reports that provisioning succeeded and then shows
  the device's IP address. It never prints the SSID, Wi-Fi password, or BLE PIN.
- Confirm the ESP32 can reach the existing backend at `API_BASE`; the dashboard
  should show `RADAR-01` online within its 30-second heartbeat window.
- If the app reports an incorrect password or cannot find the network, check
  the network credentials and ensure the access point provides 2.4 GHz Wi-Fi.
  Retry in the app while BLE provisioning is active, or hold the button again
  for 3 seconds to start a fresh session.
- To inspect logs, connect USB and open Serial Monitor at 115200 baud. Do not
  connect USB and an external 5 V source simultaneously unless the board
  explicitly supports powering it that way.

The provisioning transport and BLE app flow are verified against the bundled
Arduino Core API and compile-tested. Actual iPhone pairing and RF behavior still
require testing with the physical board and phone.
