import { ChevronDown, LogOut } from "lucide-react";
import { Suspense, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, Outlet, useLocation } from "react-router-dom";

import { useAuth } from "../auth/auth-context";
import { V2Lang } from "./Lang";
import { V2Logo } from "./Logo";
import { NAV, type NavItem } from "./nav";
import { Spin, V2ToastProvider } from "./ui";

const COLLAPSE_KEY = "tuningpad_nav_collapsed";

function readCollapsed(): Record<string, boolean> {
  try {
    return JSON.parse(localStorage.getItem(COLLAPSE_KEY) ?? "{}") as Record<string, boolean>;
  } catch {
    return {};
  }
}

function isActive(item: NavItem, pathname: string): boolean {
  if (item.end) return pathname === item.to;
  return pathname === item.to || pathname.startsWith(`${item.to}/`);
}

/** Console chrome: 56px top bar + grouped, collapsible 216px sidebar (launchpad V2). */
export function Shell() {
  const { t } = useTranslation();
  const location = useLocation();
  const { authRequired, logout } = useAuth();
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>(readCollapsed);

  const toggle = (key: string) =>
    setCollapsed((prev) => {
      const next = { ...prev, [key]: !prev[key] };
      try {
        localStorage.setItem(COLLAPSE_KEY, JSON.stringify(next));
      } catch {
        // per-browser convenience only
      }
      return next;
    });

  return (
    <div className="v2" data-testid="tp-shell">
      <V2ToastProvider>
        <header className="v2-top">
          <Link to="/" className="v2-brand">
            <V2Logo className="v2-brand-logo" />
            {t("v2.brand")}
          </Link>
          <nav className="v2-top-tabs" aria-label={t("nav.products")}>
            <span className="on">{t("nav.productRl")}</span>
          </nav>
          <div className="v2-top-right">
            <V2Lang />
            <div className="v2-user">
              <span className="v2-avatar">O</span>
              <span>{t("nav.operator")}</span>
              {authRequired && (
                <button
                  type="button"
                  className="v2-link"
                  onClick={() => void logout()}
                  title={t("auth.logout")}
                  aria-label={t("auth.logout")}
                >
                  <LogOut size={14} />
                </button>
              )}
            </div>
          </div>
        </header>
        <div className="v2-layout">
          <aside className="v2-side" aria-label={t("nav.label")}>
            {NAV.map((group) => {
              const closed = collapsed[group.key] === true;
              return (
                <div key={group.key} className="v2-side-group">
                  <button type="button" className="v2-side-head" aria-expanded={!closed} onClick={() => toggle(group.key)}>
                    {t(group.labelKey)}
                    <ChevronDown size={14} className={closed ? "chev closed" : "chev"} aria-hidden="true" />
                  </button>
                  {!closed &&
                    group.items.map((item) => {
                      const Icon = item.icon;
                      const active = isActive(item, location.pathname);
                      return (
                        <Link
                          key={item.to}
                          to={item.to}
                          className={active ? "v2-side-item active" : "v2-side-item"}
                          aria-current={active ? "page" : undefined}
                          data-testid={`tp-nav-${item.to}`}
                        >
                          <Icon size={16} aria-hidden="true" />
                          {t(item.labelKey)}
                        </Link>
                      );
                    })}
                </div>
              );
            })}
          </aside>
          <div className="v2-main">
            <div className="v2-main-inner">
              <Suspense fallback={<Spin />}>
                <Outlet />
              </Suspense>
            </div>
          </div>
        </div>
      </V2ToastProvider>
    </div>
  );
}
