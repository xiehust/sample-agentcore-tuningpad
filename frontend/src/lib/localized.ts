import { useTranslation } from "react-i18next";

import type { Localized } from "./api";

/** Pick the current language from a {en, zh-CN} pair (template metadata). */
export function useLocalized() {
  const { i18n } = useTranslation();
  return (l: Localized | undefined) => (l ? (i18n.language.startsWith("zh") ? l["zh-CN"] : l.en) : "");
}
