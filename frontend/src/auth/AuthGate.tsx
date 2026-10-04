import { type FormEvent, type ReactNode, useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, AUTH_UNAUTHORIZED_EVENT, errorMessage } from "../lib/api";
import { V2Lang } from "../v2/Lang";
import { V2Logo } from "../v2/Logo";
import { Field, Spin } from "../v2/ui";
import { AuthContext } from "./auth-context";

/** Resolves the session before the console renders; shows sign-in when required. */
export function AuthGate({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const [state, setState] = useState<"loading" | "in" | "out">("loading");
  const [authRequired, setAuthRequired] = useState(false);
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const s = await api.authStatus();
      setAuthRequired(s.auth_required);
      setState(s.authenticated ? "in" : "out");
    } catch (err) {
      setError(errorMessage(err));
      setState("out");
    }
  }, []);

  useEffect(() => {
    document.body.classList.add("v2-body");
    void refresh();
    const onUnauthorized = () => setState("out");
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, onUnauthorized);
  }, [refresh]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(password);
      setPassword("");
      await refresh();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const logout = useCallback(async () => {
    await api.logout().catch(() => undefined);
    setState("out");
  }, []);

  if (state === "loading") {
    return (
      <div className="v2 v2-auth-loading" role="status">
        <Spin label={t("v2.common.loading")} />
      </div>
    );
  }
  if (state === "out") {
    return (
      <div className="v2 v2-auth" data-testid="tp-login">
        <header className="v2-top">
          <span className="v2-brand">
            <V2Logo className="v2-brand-logo" />
            {t("v2.brand")}
          </span>
          <div className="v2-top-right">
            <V2Lang />
          </div>
        </header>
        <main className="v2-auth-main">
          <div className="v2-auth-split">
            <div className="v2-auth-hero">
              <h1>{t("auth.heroTitle")}</h1>
              <p>{t("auth.heroDesc")}</p>
            </div>
            <section className="v2-card v2-auth-card">
              <div className="v2-card-body">
                <h2>{t("auth.title")}</h2>
                <p className="sub">{t("auth.subtitle")}</p>
                <form className="v2-auth-fields" onSubmit={(e) => void submit(e)}>
                  <Field label={t("auth.password")}>
                    <input
                      className="v2-input"
                      type="password"
                      autoFocus
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      data-testid="tp-login-password"
                    />
                  </Field>
                  <div className="v2-auth-error">{error}</div>
                  <button type="submit" className="v2-btn primary v2-auth-submit" disabled={busy || !password}>
                    {t("auth.signIn")}
                  </button>
                </form>
              </div>
            </section>
          </div>
        </main>
      </div>
    );
  }
  return <AuthContext.Provider value={{ authRequired, logout }}>{children}</AuthContext.Provider>;
}
