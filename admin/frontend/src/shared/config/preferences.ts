export type ThemePreference = "dark" | "light";
export type DensityPreference = "compact" | "default";

const THEME_KEY = "ih-admin:theme";
const DENSITY_KEY = "ih-admin:density";

export interface SafePreferences {
  theme: ThemePreference;
  density: DensityPreference;
}

export function loadPreferences(
  storage: Pick<Storage, "getItem">,
): SafePreferences {
  const theme = storage.getItem(THEME_KEY);
  const density = storage.getItem(DENSITY_KEY);
  return {
    theme: theme === "dark" ? "dark" : "light",
    density: density === "compact" ? "compact" : "default",
  };
}

export function saveTheme(
  storage: Pick<Storage, "setItem">,
  theme: ThemePreference,
): void {
  storage.setItem(THEME_KEY, theme);
}

export function saveDensity(
  storage: Pick<Storage, "setItem">,
  density: DensityPreference,
): void {
  storage.setItem(DENSITY_KEY, density);
}
