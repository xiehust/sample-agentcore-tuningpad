import {
  Bot,
  Boxes,
  Database,
  FlaskConical,
  Gauge,
  House,
  Layers,
  PackageOpen,
  Rocket,
  Server,
  Settings,
  type LucideIcon,
} from "lucide-react";

export interface NavItem {
  to: string;
  labelKey: string;
  icon: LucideIcon;
  /** exact-match active state (the index page) */
  end?: boolean;
}

export interface NavGroup {
  key: string;
  labelKey: string;
  items: NavItem[];
}

export const NAV: NavGroup[] = [
  { key: "home", labelKey: "nav.groupHome", items: [{ to: "/", labelKey: "nav.overview", icon: House, end: true }] },
  { key: "infra", labelKey: "nav.groupInfra", items: [{ to: "/clusters", labelKey: "nav.clusters", icon: Server }] },
  {
    key: "build",
    labelKey: "nav.groupBuild",
    items: [
      { to: "/agents", labelKey: "nav.agents", icon: Bot },
      { to: "/datasets", labelKey: "nav.datasets", icon: Database },
    ],
  },
  {
    key: "train",
    labelKey: "nav.groupTrain",
    items: [
      { to: "/models", labelKey: "nav.models", icon: Layers },
      { to: "/runs", labelKey: "nav.runs", icon: Rocket },
    ],
  },
  {
    key: "deploy",
    labelKey: "nav.groupDeploy",
    items: [
      { to: "/exports", labelKey: "nav.exports", icon: PackageOpen },
      { to: "/inference", labelKey: "nav.inference", icon: Boxes },
      { to: "/evals", labelKey: "nav.evals", icon: FlaskConical },
    ],
  },
  {
    key: "admin",
    labelKey: "nav.groupAdmin",
    items: [
      { to: "/resources", labelKey: "nav.resources", icon: Gauge },
      { to: "/settings", labelKey: "nav.settings", icon: Settings },
    ],
  },
];
