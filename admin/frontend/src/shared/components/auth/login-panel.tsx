import type { SyntheticEvent } from "react";

import { AdminErrorState } from "@/shared/components/states/admin-error-state";
import { Button } from "@/shared/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/shared/ui/card";
import { Input } from "@/shared/ui/input";

export function LoginPanel({
  error,
  isPending,
  onSubmit,
}: {
  error: unknown;
  isPending: boolean;
  onSubmit: (credentials: { username: string; password: string }) => void;
}) {
  function submit(event: SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const username = data.get("username");
    const password = data.get("password");
    onSubmit({
      username: typeof username === "string" ? username : "",
      password: typeof password === "string" ? password : "",
    });
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-md items-center px-4 py-8">
      <Card className="w-full">
        <CardHeader>
          <h1>
            <CardTitle>Вход в Identity Hub</CardTitle>
          </h1>
          <CardDescription>
            Используйте учётную запись администратора.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4">
          {error === null ? null : <AdminErrorState error={error} />}
          <form className="grid gap-4" onSubmit={submit}>
            <label className="grid gap-2 text-sm font-medium">
              Имя пользователя
              <Input name="username" autoComplete="username" required />
            </label>
            <label className="grid gap-2 text-sm font-medium">
              Пароль
              <Input
                name="password"
                type="password"
                autoComplete="current-password"
                required
              />
            </label>
            <Button type="submit" disabled={isPending}>
              {isPending ? "Входим…" : "Войти"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </main>
  );
}
