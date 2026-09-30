import { RouterProvider } from "@tanstack/react-router";

import { useAuth } from "@/app/providers";
import { router } from "@/app/router";
import { Skeleton } from "@/shared/ui/skeleton";

export function App() {
  const auth = useAuth();
  if (auth.isPending)
    return <Skeleton className="h-screen w-full" aria-label="Загрузка" />;
  return <RouterProvider router={router} context={{ auth }} />;
}
