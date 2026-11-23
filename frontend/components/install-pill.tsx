"use client";

import { Download, PackageOpen } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { CommandSnippet } from "@/components/command-snippet";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth-store";
import { flatpakRefUrl, installCommand, useRepoInfo } from "@/lib/repo-store";
import { cn } from "@/lib/utils";

type Mode = "deeplink" | "download";

type Props = {
  /** File name of the latest ``.flatpak`` bundle (offline install). */
  apkFileName?: string;
  /** Bundle row id, exchanged for a signed download URL in private mode. */
  apkId?: string;
  /** Flatpak application ID — drives the ``.flatpakref`` and the command. */
  packageName?: string;
  size?: "md" | "lg" | "xl";
  /** ``deeplink`` (default): Install (.flatpakref) + command + bundle.
   *  ``download``: the single big Install pill (detail-page hero). */
  mode?: Mode;
  className?: string;
};

/* Install CTA. ``.flatpakref`` opens GNOME Software / KDE Discover (or
 * ``flatpak install --from``) and adds the remote on the fly; the command
 * is the terminal route; the bundle is the offline route. */
export function InstallPill({
  apkFileName,
  apkId,
  packageName,
  size = "lg",
  mode = "deeplink",
  className,
}: Props) {
  const { t } = useTranslation();
  const repo = useRepoInfo();
  const { user } = useAuth();
  const [busy, setBusy] = useState(false);
  const refLink = packageName ? flatpakRefUrl(repo.url, packageName) : null;
  const bundleLink = apkFileName ? `${repo.url.replace(/\/+$/, "")}/${apkFileName}` : null;

  async function onClickBundle(e: React.MouseEvent<HTMLAnchorElement>) {
    if (!user || !apkId) return;
    e.preventDefault();
    setBusy(true);
    try {
      const { url } = await api.apps.downloadUrl(apkId);
      if (!/^https?:\/\//i.test(url) && !url.startsWith("/")) throw new Error("unsafe download URL");
      window.location.href = url;
    } catch {
      if (bundleLink) window.location.href = bundleLink;
    } finally {
      setBusy(false);
    }
  }

  const primary = refLink ?? bundleLink;
  if (!primary) return null;

  const mainButton = (
    <Button
      asChild
      variant="filled"
      size={size}
      className={cn(
        "group relative overflow-hidden",
        size === "xl" && "h-14 px-8 text-base",
        mode === "download" && className,
      )}
    >
      <a href={primary} onClick={refLink ? undefined : onClickBundle} download={!refLink || undefined}>
        <Download className="h-4 w-4 transition-transform duration-300 group-hover:translate-y-0.5" strokeWidth={2.4} />
        <span>{busy ? "…" : t("appDetail.install")}</span>
      </a>
    </Button>
  );

  if (mode === "download") return mainButton;

  return (
    <div className={cn("flex flex-col items-stretch gap-3", className)}>
      {mainButton}
      {packageName && <CommandSnippet command={installCommand(packageName)} />}
      {bundleLink && (
        <Button asChild variant="tonal" size="sm" className="self-stretch" disabled={busy}>
          <a href={bundleLink} onClick={onClickBundle} download className="text-xs">
            <PackageOpen className="h-3.5 w-3.5" strokeWidth={2.2} />
            {busy ? "…" : t("appDetail.orDownloadApk")}
          </a>
        </Button>
      )}
    </div>
  );
}
