import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { PageHeader } from "../v2/ui";

export default function NotFoundPage() {
  const { t } = useTranslation();
  return (
    <>
      <PageHeader title={t("pages.notFound.title")} />
      <Link to="/" className="v2-link">
        {t("pages.notFound.home")}
      </Link>
    </>
  );
}
