import assert from "node:assert/strict";
import { rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { ESLint } from "eslint";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const eslint = new ESLint({ cwd: root });
const fixtureId = `${String(process.pid)}-${String(Date.now())}`;
const stylePath = path.join(root, "src/pages", `__lint_style_${fixtureId}.tsx`);
const boundaryPath = path.join(
  root,
  "src/pages",
  `__lint_boundary_${fixtureId}.tsx`,
);

try {
  await Promise.all([
    writeFile(
      stylePath,
      'export function Fixture() { return <div className="bg-primary p-4">x</div>; }\n',
      "utf8",
    ),
    writeFile(
      boundaryPath,
      'import { App } from "@/app/App"; export const Fixture = App;\n',
      "utf8",
    ),
  ]);

  const results = await eslint.lintFiles([stylePath, boundaryPath]);
  const styleResult = results.find((result) => result.filePath === stylePath);
  const boundaryResult = results.find(
    (result) => result.filePath === boundaryPath,
  );

  assert(styleResult, "Style lint fixture did not run");
  assert(boundaryResult, "Boundary lint fixture did not run");
  assert(
    styleResult.messages.some(
      (message) =>
        message.ruleId === "better-tailwindcss/no-restricted-classes",
    ),
    "Forbidden local visual style was not rejected",
  );
  assert(
    boundaryResult.messages.some(
      (message) => message.ruleId === "boundaries/dependencies",
    ),
    "Upward layer import was not rejected",
  );

  console.log("Architecture lint fixtures rejected as expected.");
} finally {
  await Promise.all([
    rm(stylePath, { force: true }),
    rm(boundaryPath, { force: true }),
  ]);
}
