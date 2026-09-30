import { defineConfig } from "@hey-api/openapi-ts";

export default defineConfig({
  input: "../openapi.json",
  output: {
    path: "src/shared/api/generated",
    clean: true,
    postProcess: ["prettier"],
  },
  plugins: [
    "@hey-api/typescript",
    {
      name: "@hey-api/client-fetch",
      bundle: true,
      throwOnError: false,
    },
    {
      name: "@hey-api/sdk",
      client: "@hey-api/client-fetch",
      operations: { nesting: "operationId" },
    },
  ],
});
