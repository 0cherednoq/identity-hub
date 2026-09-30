import { Link, Outlet, useNavigate, useParams } from "@tanstack/react-router";
import { Library, LogOut, Moon, Settings2, Sun, UserRound } from "lucide-react";

import { useAuth, usePreferences } from "@/app/providers";
import { logout } from "@/shared/api/auth";
import {
  adminQueryClient,
  clearSessionCache,
  invalidateSpaceQueries,
} from "@/shared/api/query-client";
import { Button } from "@/shared/ui/button";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/shared/ui/breadcrumb";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/shared/ui/dropdown-menu";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/select";

export function AppShell() {
  const auth = useAuth();
  const preferences = usePreferences();
  const navigate = useNavigate();
  const params = useParams({ strict: false });
  const currentSpace = "spaceSlug" in params ? params.spaceSlug : "";
  const allowedSpaces = (auth.principal?.allowed_spaces ?? []).filter(
    (space) => space !== "*",
  );
  const spaces =
    allowedSpaces.length > 0
      ? allowedSpaces
      : currentSpace
        ? [currentSpace]
        : [];
  const selectedSpace = currentSpace || spaces[0] || "";

  async function changeSpace(nextSpace: string) {
    if (currentSpace !== "")
      await invalidateSpaceQueries(adminQueryClient, currentSpace);
    await navigate({
      to: "/spaces/$spaceSlug",
      params: { spaceSlug: nextSpace },
      search: {},
    });
  }

  async function signOut() {
    try {
      await logout();
    } finally {
      clearSessionCache(adminQueryClient);
      await navigate({ to: "/login", search: { redirect: "/" } });
    }
  }

  return (
    <div className="bg-background min-h-screen">
      <header className="bg-background sticky top-0 z-20 border-b">
        <div className="mx-auto flex max-w-7xl items-center gap-4 px-4 py-3 sm:px-6 lg:px-8">
          <Link
            className="flex items-center gap-2 font-semibold"
            to="/"
            aria-label="Identity Hub"
          >
            <Library className="size-5" aria-hidden="true" />
            <span className="hidden sm:inline">Identity Hub</span>
          </Link>
          <nav
            className="ml-auto flex items-center gap-2"
            aria-label="Основная навигация"
          >
            <Link
              className="hover:bg-accent hidden rounded-md px-3 py-2 text-sm font-medium md:inline-flex"
              to="/"
            >
              Обзор
            </Link>
            <Link
              className="hover:bg-accent hidden rounded-md px-3 py-2 text-sm font-medium lg:inline-flex"
              to="/spaces"
              search={{}}
            >
              Пространства
            </Link>
            <Link
              className="hover:bg-accent hidden rounded-md px-3 py-2 text-sm font-medium xl:inline-flex"
              to="/services"
            >
              Сервисы
            </Link>
            <Link
              className="hover:bg-accent hidden rounded-md px-3 py-2 text-sm font-medium 2xl:inline-flex"
              to="/background"
            >
              Фоновые работы
            </Link>
            {currentSpace === "" ? null : (
              <>
                <Link
                  className="hover:bg-accent hidden rounded-md px-3 py-2 text-sm font-medium xl:inline-flex"
                  to="/spaces/$spaceSlug/accounts"
                  params={{ spaceSlug: currentSpace }}
                  search={{}}
                >
                  Аккаунты
                </Link>
                <Link
                  className="hover:bg-accent hidden rounded-md px-3 py-2 text-sm font-medium 2xl:inline-flex"
                  to="/spaces/$spaceSlug/imports"
                  params={{ spaceSlug: currentSpace }}
                >
                  Импорт
                </Link>
                <Link
                  className="hover:bg-accent hidden rounded-md px-3 py-2 text-sm font-medium xl:inline-flex"
                  to="/spaces/$spaceSlug/leases"
                  params={{ spaceSlug: currentSpace }}
                  search={{}}
                >
                  Аренды
                </Link>
                <Link
                  className="hover:bg-accent hidden rounded-md px-3 py-2 text-sm font-medium 2xl:inline-flex"
                  to="/spaces/$spaceSlug/analytics"
                  params={{ spaceSlug: currentSpace }}
                  search={{}}
                >
                  Аналитика
                </Link>
                <Link
                  className="hover:bg-accent hidden rounded-md px-3 py-2 text-sm font-medium 2xl:inline-flex"
                  to="/spaces/$spaceSlug/human-queue"
                  params={{ spaceSlug: currentSpace }}
                  search={{}}
                >
                  Нужен человек
                </Link>
                <Link
                  className="hover:bg-accent hidden rounded-md px-3 py-2 text-sm font-medium 2xl:inline-flex"
                  to="/spaces/$spaceSlug/proxies"
                  params={{ spaceSlug: currentSpace }}
                  search={{}}
                >
                  Прокси
                </Link>
                <Link
                  className="hover:bg-accent hidden rounded-md px-3 py-2 text-sm font-medium 2xl:inline-flex"
                  to="/spaces/$spaceSlug/events"
                  params={{ spaceSlug: currentSpace }}
                  search={{}}
                >
                  События
                </Link>
                <Link
                  className="hover:bg-accent hidden rounded-md px-3 py-2 text-sm font-medium 2xl:inline-flex"
                  to="/spaces/$spaceSlug/delivery-center"
                  params={{ spaceSlug: currentSpace }}
                  search={{}}
                >
                  Доставки
                </Link>
              </>
            )}
            {spaces.length === 0 ? null : (
              <Select
                value={selectedSpace}
                onValueChange={(value) => void changeSpace(value)}
              >
                <SelectTrigger aria-label="Пространство" className="w-40">
                  <SelectValue placeholder="Пространство" />
                </SelectTrigger>
                <SelectContent>
                  {spaces.map((space) => (
                    <SelectItem key={space} value={space}>
                      {space}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  type="button"
                  size="icon"
                  variant="ghost"
                  aria-label="Меню пользователя"
                >
                  <UserRound />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuLabel>
                  {auth.principal?.actor ?? "Пользователь"}
                </DropdownMenuLabel>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  onSelect={() => {
                    preferences.setTheme(
                      preferences.theme === "light" ? "dark" : "light",
                    );
                  }}
                >
                  {preferences.theme === "light" ? <Moon /> : <Sun />}
                  {preferences.theme === "light"
                    ? "Тёмная тема"
                    : "Светлая тема"}
                </DropdownMenuItem>
                <DropdownMenuItem
                  onSelect={() => {
                    preferences.setDensity(
                      preferences.density === "default" ? "compact" : "default",
                    );
                  }}
                >
                  <Settings2 /> Плотность:{" "}
                  {preferences.density === "default" ? "обычная" : "компактная"}
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem onSelect={() => void signOut()}>
                  <LogOut /> Выйти
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </nav>
        </div>
      </header>
      <div className="mx-auto max-w-7xl px-4 pt-4 sm:px-6 lg:px-8">
        <Breadcrumb>
          <BreadcrumbList>
            <BreadcrumbItem>
              <BreadcrumbLink asChild>
                <Link to="/">Главная</Link>
              </BreadcrumbLink>
            </BreadcrumbItem>
            {currentSpace === "" ? null : (
              <>
                <BreadcrumbSeparator />
                <BreadcrumbItem>
                  <BreadcrumbPage>{currentSpace}</BreadcrumbPage>
                </BreadcrumbItem>
              </>
            )}
          </BreadcrumbList>
        </Breadcrumb>
      </div>
      <Outlet />
    </div>
  );
}
