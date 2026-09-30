import type { ReactNode } from "react";

import { PageContainer, PageHeader } from "@/shared/components/layout/layout";

export interface PageTemplateProps {
  eyebrow?: string;
  title: string;
  description?: string;
  actions?: ReactNode;
  children: ReactNode;
}

function Template({
  eyebrow,
  title,
  description,
  actions,
  children,
  kind,
}: PageTemplateProps & { kind: "dashboard" | "detail" | "list" }) {
  return (
    <PageContainer>
      <div data-page-template={kind}>
        <PageHeader
          title={title}
          {...(eyebrow === undefined ? {} : { eyebrow })}
          {...(description === undefined ? {} : { description })}
          {...(actions === undefined ? {} : { actions })}
        />
      </div>
      {children}
    </PageContainer>
  );
}

export function DashboardPage(props: PageTemplateProps) {
  return <Template {...props} kind="dashboard" />;
}

export function ListPage(props: PageTemplateProps) {
  return <Template {...props} kind="list" />;
}

export function DetailPage(props: PageTemplateProps) {
  return <Template {...props} kind="detail" />;
}
