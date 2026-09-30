import boundaries from "@boundaries/eslint-plugin";
import js from "@eslint/js";
import betterTailwind from "eslint-plugin-better-tailwindcss";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";

const layerPolicies = [
  ["app", ["app", "pages", "features", "entities", "shared"]],
  ["pages", ["features", "entities", "shared"]],
  ["features", ["entities", "shared"]],
  ["entities", ["shared"]],
  ["shared", ["shared"]],
].map(([from, allowed]) => ({
  from: { element: { type: from } },
  allow: { to: { element: { types: { anyOf: allowed } } } },
}));

const arbitraryAndSpacingRestrictions = [
  {
    pattern: "(^|:)[a-z-]+-\\[[^&].*\\]",
    message: "Произвольные значения запрещены — добавьте семантический токен.",
  },
  {
    pattern: "^(p|m|gap|space)[xytrbl]?-(5|7|9|10|11|12|14|16|20|24)$",
    message: "Значение не входит в шкалу отступов дизайн-системы.",
  },
];

const localVisualRestrictions = [
  {
    pattern: "^(bg|text|border|ring|fill|stroke|outline)-",
    message: "Визуальный стиль задаётся только в shared/.",
  },
  { pattern: "^rounded", message: "Радиус задаёт компонент." },
  { pattern: "^shadow", message: "Тень задаёт компонент." },
  {
    pattern: "^(text-(xs|sm|base|lg|xl|2xl)|font-)",
    message: "Типографика задаётся компонентом.",
  },
  {
    pattern: "^border(-\\d)?$",
    message: "Рамка задаётся компонентом.",
  },
  {
    pattern: "^(p|px|py|pt|pr|pb|pl|m|mx|my|mt|mr|mb|ml)-",
    message: "Отступы задаются компонентом или PageContainer.",
  },
  { pattern: "^dark:", message: "Тёмная тема задаётся токенами." },
];

const forbiddenElements = [
  "button",
  "input",
  "select",
  "table",
  "textarea",
].map((element) => ({
  selector: `JSXOpeningElement[name.name='${element}']`,
  message: `Сырой <${element}> запрещён — используйте shared/ui.`,
}));

export default tseslint.config(
  {
    ignores: [
      "../src/identity_hub_admin/static",
      "coverage",
      "src/shared/api/generated",
    ],
  },
  {
    extends: [js.configs.recommended, ...tseslint.configs.strictTypeChecked],
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    plugins: {
      boundaries,
      "better-tailwindcss": betterTailwind,
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    settings: {
      "import/resolver": {
        typescript: {
          alwaysTryTypes: true,
          project: "tsconfig.app.json",
        },
      },
      "better-tailwindcss": {
        entryPoint: "src/styles/globals.css",
        tsconfig: "tsconfig.app.json",
      },
      "boundaries/elements": [
        { type: "app", pattern: "src/app/**" },
        { type: "pages", pattern: "src/pages/**" },
        { type: "features", pattern: "src/features/**" },
        { type: "entities", pattern: "src/entities/**" },
        { type: "shared", pattern: "src/shared/**" },
      ],
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "better-tailwindcss/no-conflicting-classes": "error",
      "better-tailwindcss/no-duplicate-classes": "error",
      "better-tailwindcss/no-unknown-classes": "error",
      "boundaries/dependencies": [
        "error",
        { default: "disallow", policies: layerPolicies },
      ],
      "react-refresh/only-export-components": [
        "warn",
        { allowConstantExport: true },
      ],
    },
  },
  {
    files: ["src/shared/components/**/*.{ts,tsx}"],
    rules: {
      "better-tailwindcss/no-restricted-classes": [
        "error",
        { restrict: arbitraryAndSpacingRestrictions },
      ],
    },
  },
  {
    files: [
      "src/pages/**/*.{ts,tsx}",
      "src/features/**/*.{ts,tsx}",
      "src/entities/**/*.{ts,tsx}",
    ],
    rules: {
      "better-tailwindcss/no-restricted-classes": [
        "error",
        {
          restrict: [
            ...arbitraryAndSpacingRestrictions,
            ...localVisualRestrictions,
          ],
        },
      ],
      "no-restricted-syntax": [
        "error",
        ...forbiddenElements,
        {
          selector: "JSXAttribute[name.name='style']",
          message: "Inline style запрещён — используйте токен и компонент.",
        },
      ],
    },
  },
  {
    files: ["src/shared/ui/**/*.{ts,tsx}"],
    rules: {
      "@typescript-eslint/no-unsafe-argument": "off",
      "@typescript-eslint/no-unsafe-assignment": "off",
      "@typescript-eslint/no-unsafe-member-access": "off",
      "@typescript-eslint/no-unnecessary-condition": "off",
      "@typescript-eslint/no-unnecessary-type-assertion": "off",
      "@typescript-eslint/restrict-template-expressions": "off",
      "better-tailwindcss/no-unknown-classes": "off",
      "better-tailwindcss/no-restricted-classes": "off",
      "no-restricted-imports": "off",
      "react-refresh/only-export-components": "off",
    },
  },
  {
    files: ["src/entities/status/status.tsx"],
    rules: { "react-refresh/only-export-components": "off" },
  },
  {
    files: ["src/app/providers.tsx"],
    rules: { "react-refresh/only-export-components": "off" },
  },
  {
    files: ["src/app/router.tsx"],
    rules: { "@typescript-eslint/only-throw-error": "off" },
  },
  {
    files: ["src/shared/components/data-table/data-table.tsx"],
    rules: { "react-hooks/incompatible-library": "off" },
  },
  {
    files: ["src/**/*.{ts,tsx}"],
    ignores: [
      "src/shared/ui/**/*.{ts,tsx}",
      "src/shared/components/charts/**/*.{ts,tsx}",
    ],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          patterns: [
            {
              group: ["radix-ui", "@radix-ui/*"],
              message: "Radix доступен только через shared/ui.",
            },
            {
              group: ["recharts"],
              message: "Recharts доступен только через chart wrappers.",
            },
          ],
        },
      ],
    },
  },
);
