"use client";

import { Download, KeyRound, Monitor, TriangleAlert } from "lucide-react";
import { useTranslation } from "react-i18next";

import { CommandSnippet } from "@/components/command-snippet";
import { Button } from "@/components/ui/button";
import { flatpakRepoFileUrl, isPlainHttp, remoteAddCommand, siteBase, useRepoInfo } from "@/lib/repo-store";

/* The "Add this remote" invitation: one command for the terminal, one
 * ``.flatpakrepo`` file for GNOME Software / KDE Discover. Reads the LIVE
 * repo address so admin edits propagate immediately. */
export function RepoCta() {
  const { t } = useTranslation();
  const repo = useRepoInfo();

  return (
    <div
      className="surface relative overflow-hidden p-6 md:p-10"
      style={{
        backgroundImage:
          "radial-gradient(70% 90% at 100% 0%, rgb(var(--primary) / 0.18), transparent 70%), radial-gradient(60% 80% at 0% 100%, rgb(var(--accent) / 0.10), transparent 70%)",
      }}
    >
      <div className="relative grid items-start gap-8 md:grid-cols-[auto_1fr] md:gap-10">
        <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-brand-gradient text-[rgb(46_52_64)] shadow-[var(--shadow-2)]">
          <Monitor className="h-7 w-7" strokeWidth={2} />
        </div>
        <div className="min-w-0">
          <h3 className="text-2xl font-bold tracking-tight text-ink md:text-3xl">
            {t("repoCta.title")}
          </h3>
          <p className="mt-1 max-w-2xl text-sm text-ink-soft md:text-base">{t("repoCta.body")}</p>
          <CommandSnippet className="mt-5 max-w-3xl" command={remoteAddCommand(repo.url)} />
          <div className="mt-4 flex flex-wrap gap-2">
            <Button asChild variant="filled" size="lg">
              <a href={flatpakRepoFileUrl(repo.url)} download>
                <Download className="h-4 w-4" strokeWidth={2.4} />
                {t("repoCta.open")}
              </a>
            </Button>
            <Button asChild variant="outlined" size="lg">
              <a href={`${siteBase(repo.url)}/api/v1/setup/public-key`} download>
                <KeyRound className="h-4 w-4" strokeWidth={2.2} />
                {t("repoCta.publicKey", { defaultValue: "Signing key (.asc)" })}
              </a>
            </Button>
          </div>
          {repo.fingerprint && (
            <p className="mt-3 break-all font-mono text-[11px] text-ink-mute">
              GPG {repo.fingerprint.replace(/(.{4})/g, "$1 ").trim()}
            </p>
          )}
          {isPlainHttp(repo.url) && (
            <p className="mt-3 flex max-w-xl items-start gap-2 text-xs text-ink-mute">
              <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              {t("repoCta.httpWarning")}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
