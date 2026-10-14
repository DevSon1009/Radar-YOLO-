#pragma once
// ====== CHÂN GPIO (tránh GPIO 26-37: dùng cho flash/PSRAM octal của N16R8) ======
#define PIN_RADAR_RX   18   // nối vào chân TX của LD2410C
#define PIN_RADAR_TX   17   // nối vào chân RX của LD2410C
#define PIN_RADAR_OUT  16   // nối vào chân OUT của LD2410C
#define PIN_RELAY       4   // IN của module relay
#define PIN_BUZZER      5   // vào mạch driver (transistor) của buzzer
#define PIN_WIFI_PROV_BUTTON 6 // nút nhấn nối GPIO6 xuống GND, giữ 3 giây để đổi Wi-Fi

#define RADAR_BAUD      256000
#define RELAY_ACTIVE_LOW 1  // đa số module relay 5V kích mức THẤP. Nếu relay nháy ngược -> đổi thành 0
#define BUZZER_ACTIVE_LOW 1 // cac ban thu stage2/stage3 da xac nhan coi kich muc THAP
#define SIMULATE_PRESENCE_BY_DEFAULT 1 // mô phỏng phát hiện người khi LD2410C mất kết nối

// ====== THỜI GIAN ======
#define TELEMETRY_MS     1000   // gửi trạng thái radar lên backend
#define POLL_MS          1000   // hỏi backend có lệnh mới không
#define HTTP_TIMEOUT_MS  3000
#define WIFI_RETRY_MS    10000
#define WIFI_PROV_AUTO_START_MS 30000 // chỉ tự mở BLE nếu chưa lưu Wi-Fi nào
#define WIFI_PROV_BUTTON_HOLD_MS 3000
#define WIFI_PROV_SERVICE_NAME "PROV_RADAR01"
#define ALARM_MAX_MS     300000 // tự tắt còi sau 5 phút nếu mất lệnh ALARM_OFF
#define PRESENCE_CONFIRM_MS 2000
#define PRESENCE_CLEAR_MS   3000

#define FW_VERSION "0.3.0"
