"use client";

import { Check, Copy, Terminal } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";

/* A copyable shell command, styled like a Nord terminal line. */
export function CommandSnippet({
  command,
  className,
  label,
}: {
  command: string;
  className?: string;
  label?: string;
}) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const id = setTimeout(() => setCopied(false), 1800);
    return () => clearTimeout(id);
  }, [copied]);

  async function copy() {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
    } catch {/* clipboard blocked */}
  }

  return (
    <div className={cn("group relative", className)}>
      {label && (
        <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-ink-mute">{label}</div>
      )}
      <div className="flex items-stretch overflow-hidden rounded-xl border border-outline-soft bg-[rgb(46_52_64)] text-[rgb(216_222_233)] shadow-[var(--shadow-1)]">
        <span className="flex items-center px-3 text-[rgb(226_164_212)]">
          <Terminal className="h-4 w-4" strokeWidth={2.2} />
        </span>
        <code className="min-w-0 flex-1 whitespace-pre-wrap break-all py-2.5 pr-2 font-mono text-[13px] leading-relaxed">
          <span className="select-none text-[rgb(136_192_208)]">$ </span>
          <span className="select-all">{command}</span>
        </code>
        <button
          type="button"
          onClick={copy}
          aria-label={t("common.copy", { defaultValue: "Copy" })}
          className="flex items-center gap-1.5 border-l border-[rgb(67_76_94)] px-3 text-xs text-[rgb(216_222_233)] transition-colors hover:bg-[rgb(59_66_82)] hover:text-[rgb(226_164_212)]"
        >
          {copied ? <Check className="h-4 w-4 text-[rgb(163_190_140)]" strokeWidth={2.4} /> : <Copy className="h-4 w-4" strokeWidth={2.2} />}
        </button>
      </div>
    </div>
  );
}
