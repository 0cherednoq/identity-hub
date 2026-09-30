import {
  AUTH_STATE_VIEW,
  AVAILABILITY_VIEW,
  DomainStatusBadge,
  LIFECYCLE_VIEW,
  STANDING_VIEW,
} from "@/entities/status/status";
import type { AccountHeaderResponse } from "@/shared/api/generated/types.gen";
import { AccountHeaderDisplay } from "@/shared/components/accounts/account-detail";

export function AccountHeader({ value }: { value: AccountHeaderResponse }) {
  return (
    <AccountHeaderDisplay
      value={value}
      statuses={
        <>
          <DomainStatusBadge
            registry={LIFECYCLE_VIEW}
            value={value.lifecycle}
          />
          <DomainStatusBadge registry={STANDING_VIEW} value={value.standing} />
          <DomainStatusBadge registry={AUTH_STATE_VIEW} value={value.auth} />
          <DomainStatusBadge
            registry={AVAILABILITY_VIEW}
            value={value.availability}
          />
        </>
      }
    />
  );
}
