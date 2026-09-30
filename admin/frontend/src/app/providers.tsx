import { QueryClientProvider, useQuery } from "@tanstack/react-query";
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { fetchPrincipal } from "@/shared/api/auth";
import { adminQueryClient } from "@/shared/api/query-client";
import { AdminApiError } from "@/shared/api/transport";
import {
  AuthContext,
  useAuth,
  type AuthState,
} from "@/shared/auth/auth-context";
import {
  loadPreferences,
  saveDensity,
  saveTheme,
  type DensityPreference,
  type ThemePreference,
} from "@/shared/config/preferences";
import { TooltipProvider } from "@/shared/ui/tooltip";

export { useAuth };
export type { AuthState };

export interface PreferencesState {
  density: DensityPreference;
  setDensity: (density: DensityPreference) => void;
  setTheme: (theme: ThemePreference) => void;
  theme: ThemePreference;
}

const PreferencesContext = createContext<PreferencesState | null>(null);

export function usePreferences(): PreferencesState {
  const context = useContext(PreferencesContext);
  if (context === null) {
    throw new Error("usePreferences must be used inside AppProviders");
  }
  return context;
}

function InnerProviders({ children }: { children: ReactNode }) {
  const stored = useMemo(() => loadPreferences(window.localStorage), []);
  const [theme, setThemeState] = useState<ThemePreference>(stored.theme);
  const [density, setDensityState] = useState<DensityPreference>(
    stored.density,
  );
  const principalQuery = useQuery({
    queryKey: ["auth", "principal"],
    queryFn: ({ signal }) => fetchPrincipal(signal),
    retry: false,
    staleTime: 60_000,
  });

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  const auth = useMemo<AuthState>(() => {
    const signedOut =
      principalQuery.error instanceof AdminApiError &&
      (principalQuery.error.status === 401 ||
        principalQuery.error.status === 403);
    return {
      error: signedOut ? null : principalQuery.error,
      isPending: principalQuery.isPending,
      principal: principalQuery.data ?? null,
    };
  }, [principalQuery.data, principalQuery.error, principalQuery.isPending]);

  const preferences = useMemo<PreferencesState>(
    () => ({
      density,
      setDensity: (next) => {
        saveDensity(window.localStorage, next);
        setDensityState(next);
      },
      setTheme: (next) => {
        saveTheme(window.localStorage, next);
        setThemeState(next);
      },
      theme,
    }),
    [density, theme],
  );

  return (
    <AuthContext.Provider value={auth}>
      <PreferencesContext.Provider value={preferences}>
        <TooltipProvider>{children}</TooltipProvider>
      </PreferencesContext.Provider>
    </AuthContext.Provider>
  );
}

export function AppProviders({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={adminQueryClient}>
      <InnerProviders>{children}</InnerProviders>
    </QueryClientProvider>
  );
}
