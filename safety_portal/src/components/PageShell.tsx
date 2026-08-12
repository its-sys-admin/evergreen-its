import type { ReactNode } from "react";
import { AppHeader } from "./AppHeader";
import { BackHomeNav } from "./BackHomeNav";
import { useAuth } from "../lib/auth";

/**
 * The singular page shell. Every authenticated page renders through this so the
 * Evergreen header (with Sign out) and the back/home control land in the SAME place
 * on every page — fixing the field-ops pages that previously rendered no <AppHeader>
 * and invented their own back control. `onHome` adds the "← Home" nav strip (omit it
 * on the Home page itself). Page content goes in the established `.page__main` well
 * (max-width, centered) like the other pages.
 *
 * `wide` opts a page into a 1240px well instead of the 920px reading default. It exists for
 * the two surfaces that are not prose — the schedule (a timeline plus a five-track task
 * grid) and the weekly production report (a section rail beside a two-column form). Every
 * other page keeps --maxw untouched.
 */
export function PageShell({
  onHome,
  wide,
  children,
}: {
  onHome?: () => void;
  wide?: boolean;
  children: ReactNode;
}) {
  const { logout } = useAuth();
  return (
    <div className="page">
      <AppHeader
        action={
          <button className="btn btn--ghost" onClick={() => void logout()}>
            Sign out
          </button>
        }
      />
      {onHome && <BackHomeNav onHome={onHome} />}
      <main className={`page__main${wide ? " page__main--wide" : ""}`}>{children}</main>
    </div>
  );
}
