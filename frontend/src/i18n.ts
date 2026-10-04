import i18n from "i18next";
import LanguageDetector from "i18next-browser-languagedetector";
import { initReactI18next } from "react-i18next";

import en from "./locales/en/common.json";
import zhCN from "./locales/zh-CN/common.json";

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: { en: { common: en }, "zh-CN": { common: zhCN } },
    fallbackLng: "en",
    supportedLngs: ["en", "zh-CN"],
    defaultNS: "common",
    interpolation: { escapeValue: false },
    detection: { order: ["localStorage", "navigator", "htmlTag"], caches: ["localStorage"] },
  });

i18n.on("languageChanged", (lng) => {
  document.documentElement.lang = lng;
});

export default i18n;
