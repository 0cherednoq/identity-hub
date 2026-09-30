import { Link, useParams } from "@tanstack/react-router";

import { ImportsContent } from "@/shared/components/imports/imports-content";
import { ListPage } from "@/shared/components/templates/templates";
import { Button } from "@/shared/ui/button";

export function ImportsPage() {
  const { spaceSlug } = useParams({
    from: "/_authenticated/spaces/$spaceSlug/imports",
  });
  return (
    <ListPage
      eyebrow="Операции с данными"
      title={`Импорт · ${spaceSlug}`}
      description="CSV и JSONL проверяются на сервере, выполняются возобновляемыми пакетами и не сохраняются как исходные файлы."
      actions={
        <Button asChild variant="outline">
          <Link
            to="/spaces/$spaceSlug/accounts"
            params={{ spaceSlug }}
            search={{}}
          >
            К аккаунтам
          </Link>
        </Button>
      }
    >
      <ImportsContent spaceSlug={spaceSlug} />
    </ListPage>
  );
}
