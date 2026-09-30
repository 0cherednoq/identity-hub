import { useQuery } from "@tanstack/react-query";
import {
  Link,
  useNavigate,
  useParams,
  useSearch,
} from "@tanstack/react-router";

import {
  fetchAccount,
  fetchAccountSection,
  type AccountSection,
} from "@/shared/api/accounts";
import {
  AccountSectionPanel,
  AccountTabs,
} from "@/shared/components/accounts/account-detail";
import { AccountHeader } from "@/features/accounts/account-detail";
import { QueryState } from "@/shared/components/states/query-state";
import { AccountActions } from "@/shared/components/commands/account-actions";
import { SecretControls } from "@/shared/components/commands/secret-controls";
import { ListPage } from "@/shared/components/templates/templates";
import { Button } from "@/shared/ui/button";
import { Skeleton } from "@/shared/ui/skeleton";

export function AccountDetailPage() {
  const { identityId, spaceSlug } = useParams({
    from: "/_authenticated/spaces/$spaceSlug/accounts/$identityId",
  });
  const search = useSearch({
    from: "/_authenticated/spaces/$spaceSlug/accounts/$identityId",
  });
  const navigate = useNavigate({
    from: "/spaces/$spaceSlug/accounts/$identityId",
  });
  const tab: AccountSection = search.tab ?? "state";
  const header = useQuery({
    queryKey: ["space", spaceSlug, "account", identityId, "header"],
    queryFn: ({ signal }) => fetchAccount(spaceSlug, identityId, signal),
  });
  const section = useQuery({
    queryKey: ["space", spaceSlug, "account", identityId, "section", tab],
    queryFn: ({ signal }) =>
      fetchAccountSection(spaceSlug, identityId, tab, signal),
    enabled: header.isSuccess,
  });

  return (
    <ListPage
      eyebrow="Аккаунт"
      title={header.data?.external_ref ?? identityId}
      description="Карточка аккаунта с действиями через command gateway, обязательной причиной и revision guard."
      actions={
        <Button asChild variant="outline">
          <Link to="/spaces/$spaceSlug/accounts" params={{ spaceSlug }}>
            К списку
          </Link>
        </Button>
      }
    >
      <QueryState
        query={header}
        skeleton={<Skeleton className="h-48 w-full" />}
        empty={{ title: "Аккаунт не найден", description: "Проверьте ссылку." }}
        isEmpty={() => false}
      >
        {(value) => <AccountHeader value={value} />}
      </QueryState>
      {header.isSuccess ? (
        <>
          <AccountTabs
            value={tab}
            onChange={(next) => {
              void navigate({ search: { tab: next } });
            }}
          />
          <QueryState
            query={section}
            skeleton={<Skeleton className="h-96 w-full" />}
            empty={{ title: "Нет данных", description: "Раздел пока пуст." }}
            isEmpty={() => false}
          >
            {(value) => (
              <div className="grid gap-4">
                <AccountSectionPanel value={value} />
                <AccountActions
                  actions={value.actions ?? []}
                  identityId={identityId}
                  revision={value.revision}
                  spaceSlug={spaceSlug}
                  onReload={() => {
                    void Promise.all([header.refetch(), section.refetch()]);
                  }}
                />
                <SecretControls
                  descriptors={value.secrets ?? []}
                  identityId={identityId}
                  revision={value.revision}
                  spaceSlug={spaceSlug}
                  onReload={() => {
                    void Promise.all([header.refetch(), section.refetch()]);
                  }}
                />
              </div>
            )}
          </QueryState>
        </>
      ) : null}
    </ListPage>
  );
}
