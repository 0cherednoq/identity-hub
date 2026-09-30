import { useEffect, useState, type ReactNode } from "react";

import {
  DomainStatusBadge,
  STATUS_GROUPS,
  type StatusRegistry,
} from "@/entities/status/status";
import { DesignCatalog } from "@/shared/components/catalog/design-catalog";
import { loadPreferences, saveTheme } from "@/shared/config/preferences";

function statusCatalog(): ReactNode {
  return STATUS_GROUPS.map((group) => (
    <section className="grid gap-2" key={group.label}>
      <h3>{group.label}</h3>
      <div className="flex flex-wrap gap-2">
        {group.values.map((value) => (
          <DomainStatusBadge
            key={value}
            registry={group.view as StatusRegistry<string>}
            value={value}
          />
        ))}
      </div>
    </section>
  ));
}

export function DesignPage() {
  const params = new URLSearchParams(window.location.search);
  const [theme, setTheme] = useState(() =>
    params.get("theme") === "dark"
      ? "dark"
      : loadPreferences(window.localStorage).theme,
  );
  const visualTest = params.get("visual") === "1";

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    document.documentElement.dataset.theme = theme;
    document.documentElement.classList.toggle("visual-test", visualTest);
  }, [theme, visualTest]);
  return (
    <DesignCatalog
      theme={theme}
      onThemeChange={() => {
        const next = theme === "light" ? "dark" : "light";
        saveTheme(window.localStorage, next);
        document.documentElement.classList.toggle("dark", next === "dark");
        document.documentElement.dataset.theme = next;
        setTheme(next);
      }}
      domainStatuses={statusCatalog()}
    />
  );
}

export default DesignPage;
