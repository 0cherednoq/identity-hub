# Browser quality harness

The suite starts `identity_hub_admin.testing:app`. It contains deterministic bearer principals
for `viewer`, `operator`, and `admin`, plus one harmless operator-only probe used to prove that a
direct viewer request still receives `403`. Production and development entry points do not import
that module.

Visual baselines cover light/dark themes at desktop and mobile widths. Update a screenshot only
when the same commit explains the intentional UI change. Review the changed image before running:

```powershell
npm run test:visual -- --update-snapshots
```

Never update snapshots only to make CI green. A changed baseline is part of the product change and
must be reviewable alongside it.
