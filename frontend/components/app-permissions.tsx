"use client";

import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { groupFlatpakPermissions, overallRisk, type Risk } from "@/lib/flatpak-permissions";
import { cn } from "@/lib/utils";

type Props = {
  permissions: string[];
  versionLabel?: string;
};

const RISK_STYLE: Record<Risk, string> = {
  low: "bg-[rgb(var(--success)/0.18)] text-ink",
  medium: "bg-[rgb(var(--warning)/0.22)] text-ink",
  high: "bg-danger-container text-danger-on-container",
};
const RISK_LABEL: Record<Risk, string> = { low: "Low", medium: "Medium", high: "High" };

/* Flatpak sandbox permissions, grouped by subsystem with a risk grade —
 * the information GNOME Software shows as "Safety" before installing. */
export function AppPermissions({ permissions, versionLabel }: Props) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const groups = useMemo(() => groupFlatpakPermissions(permissions), [permissions]);
  const risk = useMemo(() => overallRisk(permissions), [permissions]);

  if (permissions.length === 0) {
    return (
      <p className="text-sm text-ink-soft">
        <span className={cn("mr-2 rounded-pill px-2 py-0.5 text-[11px] font-semibold", RISK_STYLE.low)}>
          {t("permissions.sandboxed", { defaultValue: "Fully sandboxed" })}
        </span>
        {t("permissions.none")}
      </p>
    );
  }

  const INITIAL_GROUPS = 4;
  const visible = expanded ? groups : groups.slice(0, INITIAL_GROUPS);
  const hidden = groups.length - visible.length;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className={cn("rounded-pill px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wider", RISK_STYLE[risk])}>
          {t(`permissions.risk.${risk}`, { defaultValue: `${RISK_LABEL[risk]} risk` })}
        </span>
        {versionLabel && (
          <span className="font-mono text-xs text-ink-mute">
            {permissions.length} · {versionLabel}
          </span>
        )}
      </div>

      <div className="grid gap-x-8 gap-y-5 md:grid-cols-2">
        {visible.map(({ key, label, icon: Icon, items }) => (
          <section key={key} className="min-w-0">
            <header className="mb-2 flex items-center gap-2.5 text-ink">
              <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-pill bg-surface-2 text-ink-soft">
                <Icon className="h-3.5 w-3.5" strokeWidth={2.2} />
              </span>
              <h4 className="text-sm font-semibold tracking-tight">{t(`permissions.flatpak.${key}`, { defaultValue: label })}</h4>
              <span className="font-mono text-[10px] text-ink-mute">{items.length}</span>
            </header>
            <ul className="space-y-1.5 pl-9">
              {items.map((item) => (
                <li key={item.raw} className="flex items-start gap-2 text-sm text-ink-soft" title={item.raw}>
                  <span
                    className={cn(
                      "mt-1.5 h-2 w-2 shrink-0 rounded-full",
                      item.risk === "high" ? "bg-danger" : item.risk === "medium" ? "bg-[rgb(var(--warning))]" : "bg-[rgb(var(--success))]",
                    )}
                  />
                  <span className="min-w-0">
                    {t(`permissions.fp.${item.key}`, {
                      ...item.vars,
                      ...(item.vars.dir ? { dir: t(`permissions.fpDirs.${item.vars.dir}`, { defaultValue: item.vars.dir }) } : {}),
                      defaultValue: item.label,
                    })}
                    <code className="ml-1.5 hidden font-mono text-[10px] text-ink-mute sm:inline">{item.raw}</code>
                  </span>
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>

      {hidden > 0 && (
        <Button type="button" variant="text" size="sm" onClick={() => setExpanded(true)}>
          {t("permissions.showAll", { count: hidden, defaultValue: `Show ${hidden} more` })}
        </Button>
      )}
    </div>
  );
}
