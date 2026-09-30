import { useMutation } from "@tanstack/react-query";
import { useSearch } from "@tanstack/react-router";

import { login } from "@/shared/api/auth";
import { adminQueryClient } from "@/shared/api/query-client";
import { LoginPanel } from "@/shared/components/auth/login-panel";

export function LoginPage() {
  const search = useSearch({ from: "/login" });
  const mutation = useMutation({
    mutationFn: (credentials: { username: string; password: string }) =>
      login(credentials),
    onSuccess: (principal) => {
      adminQueryClient.setQueryData(["auth", "principal"], principal);
      window.location.replace(search.redirect);
    },
  });

  return (
    <LoginPanel
      error={mutation.error}
      isPending={mutation.isPending}
      onSubmit={(credentials) => {
        mutation.mutate(credentials);
      }}
    />
  );
}
