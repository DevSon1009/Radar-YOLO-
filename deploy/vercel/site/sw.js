self.addEventListener("push", (event) => {
  let message = {};
  try {
    message = event.data ? event.data.json() : {};
  } catch (error) {
    message = { title: "Radar IoT", body: "Có cảnh báo mới." };
  }

  event.waitUntil(
    self.registration.showNotification(message.title || "Radar IoT", {
      body: message.body || "Có cảnh báo mới.",
      icon: "/icon.svg",
      badge: "/icon.svg",
      tag: message.tag || "radar-alert",
      renotify: true,
      data: { url: message.url || "/" },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || "/", self.location.origin).href;
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
      for (const client of windows) {
        if (client.url.startsWith(self.location.origin) && "focus" in client) {
          return client.navigate(target).then(() => client.focus());
        }
      }
      return clients.openWindow(target);
    }),
  );
});
