import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { AppHeader } from "../components/AppHeader";
import { useAuth } from "../lib/auth";
import * as api from "../lib/api";

/** Map a server error code to an operator-readable message. */
function explain(code: string): string {
  switch (code) {
    case "last_admin":
      return "That would leave no active admin — make someone else an admin first.";
    case "username_taken":
      return "That username is already taken.";
    case "exists":
      return "An account with that username already exists.";
    case "invalid_username":
    case "invalid_new_username":
      return "Username must be lastname.firstname (lowercase letters, one dot).";
    case "invalid_password":
      return "Password must be 8–256 characters.";
    case "invalid_role":
      return "Invalid role.";
    case "not_found":
      return "That account no longer exists — the list may be out of date.";
    case "no_changes":
      return "No changes to save.";
    case "forbidden":
      return "You're not authorized to do that.";
    default:
      return "Something went wrong. Please try again.";
  }
}

type Banner = { kind: "ok" | "err"; msg: string } | null;

export function AccountsPage({
  tabBar,
  onEditingChange,
}: {
  tabBar: ReactNode;
  onEditingChange?: (editing: boolean) => void;
}) {
  const { user, logout } = useAuth();
  const me = user?.username ?? "";

  const [accounts, setAccounts] = useState<api.Account[] | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [banner, setBanner] = useState<Banner>(null);
  const [busy, setBusy] = useState(false);

  // Create-account form.
  const [cuName, setCuName] = useState("");
  const [cuPass, setCuPass] = useState("");
  const [cuRole, setCuRole] = useState<api.Role>("submitter");

  // Per-row login editor (one open at a time).
  const [editing, setEditing] = useState<string | null>(null);
  const [edName, setEdName] = useState("");
  const [edPass, setEdPass] = useState("");

  async function load() {
    setLoadErr(null);
    try {
      setAccounts(await api.listAccounts());
    } catch (e) {
      setLoadErr(e instanceof api.AdminError ? explain(e.code) : "Could not load accounts.");
    }
  }

  useEffect(() => {
    void load();
  }, []);

  // Report an open per-row login editor up to the admin shell so useIdleLogout keeps the session
  // alive while editing (and reset on unmount). Login edits are quick — unlike the Forms builder,
  // little is lost on a timeout — but the uniform keep-alive contract keeps idle behaviour
  // consistent across the admin tabs.
  useEffect(() => {
    onEditingChange?.(editing !== null);
  }, [editing, onEditingChange]);
  useEffect(() => () => onEditingChange?.(false), [onEditingChange]);

  /** Run a mutation, then reauth-or-refresh. Returns true on success. */
  async function run(fn: () => Promise<api.AdminResult>, okMsg: string): Promise<boolean> {
    setBusy(true);
    setBanner(null);
    try {
      const r = await fn();
      if (r.reauth) {
        // Edited our own login/role (or deleted ourselves) — the server cleared the
        // session cookie; drop to the login screen with the new credentials.
        await logout();
        return true;
      }
      setBanner({ kind: "ok", msg: okMsg });
      await load();
      return true;
    } catch (e) {
      const msg = e instanceof api.AdminError ? explain(e.code) : "Something went wrong.";
      setBanner({ kind: "err", msg });
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function onCreate() {
    const ok = await run(
      () => api.createAccount(cuName.trim(), cuPass, cuRole),
      `Created ${cuName.trim()}.`,
    );
    if (ok) {
      setCuName("");
      setCuPass("");
      setCuRole("submitter");
    }
  }

  function openEditor(username: string) {
    setEditing(username);
    setEdName(username);
    setEdPass("");
    setBanner(null);
  }

  async function onSaveLogin(original: string) {
    const changes: { new_username?: string; new_password?: string } = {};
    const nu = edName.trim();
    if (nu && nu !== original) changes.new_username = nu;
    if (edPass) changes.new_password = edPass;
    if (changes.new_username === undefined && changes.new_password === undefined) {
      // No actual change (editor opened, maybe accidentally, nothing edited) → just
      // CLOSE it as a no-op instead of erroring + trapping it open. Blank password
      // already means "keep current", so Submit-to-close is the natural gesture.
      setEditing(null);
      return;
    }
    const ok = await run(() => api.editCredentials(original, changes), `Updated ${original}'s login.`);
    if (ok) setEditing(null);
  }

  async function onChangeRole(a: api.Account, next: api.Role) {
    if (next === a.role) return;
    await run(() => api.setRole(a.username, next), `${a.username} is now ${next}.`);
  }

  async function onDelete(username: string) {
    if (!window.confirm(`Delete ${username}? This cannot be undone.`)) return;
    await run(() => api.deleteAccount(username), `Deleted ${username}.`);
  }

  return (
    <div className="page">
      <AppHeader
        action={<button className="btn btn--ghost" onClick={() => void logout()}>Sign out</button>}
      />
      {tabBar}
      <main className="page__main">
        {banner ? (
          <p className={banner.kind === "ok" ? "banner banner--ok" : "banner banner--err"} role="status">
            {banner.msg}
          </p>
        ) : null}

        <section className="card">
          <h2 className="page__heading">Create an account</h2>
          <label className="field">
            <span className="field__label">Username (lastname.firstname)</span>
            <input
              className="field__input"
              value={cuName}
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
              maxLength={64}
              onChange={(e) => setCuName(e.target.value)}
            />
          </label>
          <label className="field">
            <span className="field__label">Temporary password (≥ 8 characters)</span>
            <input
              className="field__input"
              type="text"
              value={cuPass}
              maxLength={256}
              onChange={(e) => setCuPass(e.target.value)}
            />
          </label>
          <label className="field">
            <span className="field__label">Role</span>
            <select
              className="field__input"
              value={cuRole}
              onChange={(e) => setCuRole(e.target.value as api.Role)}
            >
              <option value="submitter">Subcontractor (field PM)</option>
              <option value="manager">Manager (crew lead)</option>
              <option value="admin">Admin (dashboard access)</option>
            </select>
          </label>
          <button
            className="btn btn--primary"
            disabled={busy || !cuName.trim() || cuPass.length < 8}
            onClick={() => void onCreate()}
          >
            {busy ? "Working…" : "Create account"}
          </button>
        </section>

        <section className="card accounts">
          <h2 className="page__heading">Accounts</h2>
          {loadErr ? (
            <p className="login__error" role="alert">{loadErr}</p>
          ) : accounts === null ? (
            <p className="muted">Loading…</p>
          ) : accounts.length === 0 ? (
            <p className="muted">No accounts yet.</p>
          ) : (
            <ul className="accounts__list">
              {accounts.map((a) => (
                <li key={a.username} className="accounts__row">
                  <div className="accounts__id">
                    <span className="accounts__name">
                      {a.username}
                      {a.username === me ? <span className="accounts__you"> (you)</span> : null}
                    </span>
                    <span className={`role-badge${a.role === "admin" ? " role-badge--admin" : a.role === "manager" ? " role-badge--manager" : ""}`}>
                      {api.roleLabel(a.role)}
                    </span>
                    {a.disabled ? <span className="role-badge role-badge--off">disabled</span> : null}
                  </div>
                  <div className="accounts__actions">
                    <button className="btn btn--edit" disabled={busy} onClick={() => openEditor(a.username)}>
                      Edit login
                    </button>
                    <select
                      className="btn btn--secondary"
                      aria-label={`Role for ${a.username}`}
                      value={a.role}
                      disabled={busy}
                      onChange={(e) => void onChangeRole(a, e.target.value as api.Role)}
                    >
                      <option value="submitter">Subcontractor</option>
                      <option value="manager">Manager</option>
                      <option value="admin">Admin</option>
                    </select>
                    <button className="btn btn--retire" disabled={busy} onClick={() => void onDelete(a.username)}>
                      Delete
                    </button>
                  </div>
                  {editing === a.username ? (
                    <div className="accounts__editor">
                      <label className="field">
                        <span className="field__label">New username</span>
                        <input
                          className="field__input"
                          value={edName}
                          autoCapitalize="none"
                          autoCorrect="off"
                          spellCheck={false}
                          maxLength={64}
                          onChange={(e) => setEdName(e.target.value)}
                        />
                      </label>
                      <label className="field">
                        <span className="field__label">New password (leave blank to keep)</span>
                        <input
                          className="field__input"
                          type="text"
                          value={edPass}
                          maxLength={256}
                          onChange={(e) => setEdPass(e.target.value)}
                        />
                      </label>
                      {a.username === me ? (
                        <p className="jha__notice">
                          Editing your own login will sign you out — you'll log back in with the new
                          credentials.
                        </p>
                      ) : null}
                      <div className="jha__actions">
                        <button className="btn btn--primary" disabled={busy} onClick={() => void onSaveLogin(a.username)}>
                          {busy ? "Saving…" : "Save login"}
                        </button>
                        <button className="btn btn--secondary" disabled={busy} onClick={() => setEditing(null)}>
                          Cancel
                        </button>
                      </div>
                    </div>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </section>
      </main>
    </div>
  );
}
