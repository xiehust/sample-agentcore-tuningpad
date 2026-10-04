import { lazy } from "react";
import { BrowserRouter, Route, Routes } from "react-router-dom";

import { Shell } from "./v2/Shell";

const page = (load: () => Promise<{ default: React.ComponentType }>) => lazy(load);

const Overview = page(() => import("./pages/Overview"));
const Clusters = page(() => import("./pages/Clusters"));
const Agents = page(() => import("./pages/Agents"));
const Datasets = page(() => import("./pages/Datasets"));
const Models = page(() => import("./pages/Models"));
const Runs = page(() => import("./pages/Runs"));
const Exports = page(() => import("./pages/Exports"));
const Inference = page(() => import("./pages/Inference"));
const Evals = page(() => import("./pages/Evals"));
const Resources = page(() => import("./pages/Resources"));
const Settings = page(() => import("./pages/Settings"));
const NotFound = page(() => import("./pages/NotFound"));

/** Sub-pages use `?view=` / `?id=` query params (launchpad convention), not nested routes. */
export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Shell />}>
          <Route index element={<Overview />} />
          <Route path="clusters" element={<Clusters />} />
          <Route path="agents" element={<Agents />} />
          <Route path="datasets" element={<Datasets />} />
          <Route path="models" element={<Models />} />
          <Route path="runs" element={<Runs />} />
          <Route path="exports" element={<Exports />} />
          <Route path="inference" element={<Inference />} />
          <Route path="evals" element={<Evals />} />
          <Route path="resources" element={<Resources />} />
          <Route path="settings" element={<Settings />} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
