import { useCallback, useEffect, useState } from "react";
import { Check, Download, RefreshCw } from "lucide-react";

import { checkForUpdate, applyUpdate, type UpdateCheckResponse } from "../../data/chat";
import { useWorkspaceI18n } from "./i18n";

export function UpdateSettings() {
  const { t } = useWorkspaceI18n();
  const [info, setInfo] = useState<UpdateCheckResponse | null>(null);
  const [busy, setBusy] = useState<"" | "check" | "apply">("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const check = useCallback(async () => {
    setBusy("check");
    setError(null);
    setNotice(null);
    try {
      const data = await checkForUpdate();
      setInfo(data);
      if (!data.update_available) {
        setNotice(t("update.upToDate"));
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Failed to check for updates");
    } finally {
      setBusy("");
    }
  }, [t]);

  useEffect(() => {
    void check();
  }, [check]);

  async function doUpdate() {
    setBusy("apply");
    setError(null);
    setNotice(null);
    try {
      const result = await applyUpdate();
      if (result.ok) {
        setNotice(t("update.success"));
        void check();
      } else {
        setError(result.stderr || "Update failed");
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Update failed");
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="personal-update-settings">
      {error ? <div className="personal-machine-error" role="alert">{error}</div> : null}
      {notice ? <div className="personal-machine-notice"><Check size={14} /> {notice}</div> : null}

      <div className="personal-update-info">
        <div className="personal-update-row">
          <span className="personal-update-label">{t("update.currentVersion")}</span>
          <strong>{info?.current_version ?? "..."}</strong>
        </div>
        {info?.latest_version ? (
          <div className="personal-update-row">
            <span className="personal-update-label">{t("update.latestVersion")}</span>
            <strong>{info.latest_version}</strong>
            {info.update_available ? (
              <span className="personal-update-available-badge">{t("update.available")}</span>
            ) : null}
          </div>
        ) : null}
      </div>

      <div className="personal-update-actions">
        <button type="button" onClick={() => void check()} disabled={busy !== ""}>
          <RefreshCw size={14} />
          <span>{busy === "check" ? t("update.checking") : t("update.checkButton")}</span>
        </button>
        {info?.update_available ? (
          <button
            type="button"
            className="personal-update-apply-button"
            onClick={() => void doUpdate()}
            disabled={busy !== ""}
          >
            <Download size={14} />
            <span>{busy === "apply" ? t("update.applying") : t("update.applyButton")}</span>
          </button>
        ) : null}
      </div>
    </div>
  );
}
