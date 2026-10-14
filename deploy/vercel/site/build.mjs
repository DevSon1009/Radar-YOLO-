import { cp, mkdir } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(fileURLToPath(import.meta.url));
const output = join(root, "dist");
const files = [
  "index.html",
  "manifest.webmanifest",
  "icon.svg",
  "sw.js",
];

await mkdir(output, { recursive: true });
await Promise.all(
  files.map((file) => cp(join(root, file), join(output, file))),
);
console.log(`Built static dashboard in ${output}`);
