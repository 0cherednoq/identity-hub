import { createContext, useContext } from "react";

import type { PrincipalResponse } from "@/shared/api/generated/types.gen";

export interface AuthState {
  error: unknown;
  isPending: boolean;
  principal: PrincipalResponse | null;
}

export const AuthContext = createContext<AuthState | null>(null);

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (context === null)
    throw new Error("useAuth must be used inside AppProviders");
  return context;
}
