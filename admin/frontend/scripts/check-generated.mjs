import { createHash } from "node:crypto";
import { readdir, readFile } from "node:fs/promises";
import { join, relative } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const project = fileURLToPath(new URL("../", import.meta.url));
const generated = join(project, "src", "shared", "api", "generated");

async function snapshot(directory, root = directory, result = new Map()) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      await snapshot(path, root, result);
    } else {
      const digest = createHash("sha256")
        .update(await readFile(path))
        .digest("hex");
      result.set(relative(root, path).replaceAll("\\", "/"), digest);
    }
  }
  return result;
}

const before = await snapshot(generated);
for (const script of [
  join(project, "node_modules", "@hey-api", "openapi-ts", "bin", "run.js"),
  join(project, "scripts", "postprocess-generated.mjs"),
]) {
  const result = spawnSync(process.execPath, [script], {
    cwd: project,
    stdio: "inherit",
  });
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}
const after = await snapshot(generated);
const changed = new Set([...before.keys(), ...after.keys()]);
const drift = [...changed]
  .filter((path) => before.get(path) !== after.get(path))
  .sort();
if (drift.length > 0) {
  console.error(
    `Generated API client drifted:\n${drift.map((path) => `- ${path}`).join("\n")}`,
  );
  process.exit(1);
}
console.log(`Generated API client is current (${after.size} files)`);
