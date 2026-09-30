import { readdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(
  new URL("../src/shared/api/generated/", import.meta.url),
);
const banner =
  "// @ts-nocheck -- bundled Hey API runtime is vendor-generated\n";

async function visit(directory) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      await visit(path);
    } else if (entry.name.endsWith(".ts")) {
      const source = await readFile(path, "utf8");
      if (!source.startsWith(banner)) {
        await writeFile(path, `${banner}${source}`, "utf8");
      }
    }
  }
}

for (const directory of ["client", "core"]) {
  await visit(join(root, directory));
}
