/* Human view of Flatpak sandbox permissions (``finish-args``).
 *
 * The backend stores each permission as its finish-arg string
 * (``--share=network``, ``--filesystem=home:ro``, ``--talk-name=org.x``).
 * We bucket them by subsystem and grade the risk the way Flathub's review
 * guidelines do: anything that escapes the sandbox is ``high``. */
import {
  Bell,
  Cpu,
  FolderOpen,
  Globe,
  type LucideIcon,
  MessagesSquare,
  Monitor,
  ShieldAlert,
  SlidersHorizontal,
  Volume2,
} from "lucide-react";

export type Risk = "low" | "medium" | "high";

export type PermissionItem = { raw: string; label: string; risk: Risk; key: string; vars: Record<string, string> };
export type PermissionGroup = { key: string; label: string; icon: LucideIcon; items: PermissionItem[] };

const GROUPS: Record<string, { label: string; icon: LucideIcon }> = {
  escape: { label: "Sandbox escape", icon: ShieldAlert },
  files: { label: "Files", icon: FolderOpen },
  network: { label: "Network", icon: Globe },
  display: { label: "Display", icon: Monitor },
  audio: { label: "Audio", icon: Volume2 },
  devices: { label: "Devices", icon: Cpu },
  bus: { label: "D-Bus services", icon: MessagesSquare },
  notify: { label: "Notifications", icon: Bell },
  other: { label: "Other", icon: SlidersHorizontal },
};

const ESCAPE_NAMES = new Set([
  "org.freedesktop.Flatpak",
  "org.freedesktop.systemd1",
  "org.freedesktop.login1",
  "org.freedesktop.secrets",
  "org.freedesktop.PolicyKit1",
]);

type Classified = { group: string; label: string; risk: Risk; key: string; vars?: Record<string, string> };

function c(group: string, key: string, label: string, risk: Risk, vars?: Record<string, string>): Classified {
  return { group, key, label, risk, vars };
}

const SOCKETS: Record<string, [string, string, Risk]> = {
  wayland: ["display", "Wayland display", "low"],
  "fallback-x11": ["display", "X11 display (only without Wayland)", "low"],
  x11: ["display", "X11 display — can read other windows' input", "medium"],
  pulseaudio: ["audio", "Audio playback and recording (PulseAudio)", "medium"],
  "session-bus": ["escape", "Unfiltered session bus", "high"],
  "system-bus": ["escape", "Unfiltered system bus", "high"],
  "ssh-auth": ["escape", "Your SSH agent", "high"],
  "gpg-agent": ["escape", "Your GPG agent", "high"],
  pcsc: ["devices", "Smart cards (PC/SC)", "medium"],
  cups: ["devices", "Printing (CUPS)", "low"],
  "inherit-wayland-socket": ["display", "Inherited Wayland socket", "low"],
};
const DEVICES: Record<string, [string, Risk]> = {
  dri: ["GPU acceleration", "low"],
  kvm: ["Virtualisation (KVM)", "medium"],
  shm: ["Shared memory (/dev/shm)", "low"],
  input: ["Input devices (gamepads, …)", "medium"],
  usb: ["Raw USB devices", "medium"],
  all: ["All devices (webcams, USB, …)", "high"],
};
const ALLOW: Record<string, [string, string, Risk]> = {
  devel: ["escape", "Debugging syscalls (ptrace, perf)", "high"],
  multiarch: ["other", "Run 32-bit code", "low"],
  bluetooth: ["devices", "Bluetooth sockets", "medium"],
  canbus: ["devices", "CAN bus sockets", "medium"],
};

function classify(arg: string): Classified {
  const [flag, value = ""] = arg.replace(/^--/, "").split(/=(.*)/s);
  switch (flag) {
    case "share":
      if (value === "network") return c("network", "share.network", "Full network access", "low");
      if (value === "ipc") return c("display", "share.ipc", "Shared memory with X11 (IPC)", "low");
      break;
    case "socket": {
      const hit = SOCKETS[value];
      if (hit) return c(hit[0], `socket.${value}`, hit[1], hit[2]);
      break;
    }
    case "device": {
      const hit = DEVICES[value];
      if (hit) return c("devices", `device.${value}`, hit[0], hit[1]);
      break;
    }
    case "filesystem": {
      const [path, mode] = value.split(":");
      const ro = mode === "ro";
      const sfx = ro ? "Ro" : "";
      const roLabel = ro ? " (read-only)" : "";
      if (path === "host" || path === "host-os" || path === "host-etc" || path === "/")
        return c("escape", `fs.host${sfx}`, `Entire host filesystem${roLabel}`, "high");
      if (path === "home" || path === "~")
        return c("files", `fs.home${sfx}`, `Your whole home folder${roLabel}`, ro ? "medium" : "high");
      const xdg = /^xdg-(download|documents|music|pictures|videos|desktop|public-share|templates)/.exec(path);
      if (xdg) return c("files", `fs.xdg${sfx}`, `${xdg[1].replace(/^\w/, (m) => m.toUpperCase())} folder${roLabel}`, "low", { dir: xdg[1] });
      if (/^xdg-(config|data|cache)/.test(path) || /^~\/\.(config|local)/.test(path))
        return c("files", `fs.appData${sfx}`, `App data of other programs: ${path}${roLabel}`, "medium", { path });
      if (path.startsWith("xdg-run/"))
        return c("files", `fs.runtime${sfx}`, `Runtime socket ${path.slice(8)}${roLabel}`, "medium", { path: path.slice(8) });
      return c("files", `fs.path${sfx}`, `${path}${roLabel}`, path.startsWith("/") ? "medium" : "low", { path });
    }
    case "persist":
      return c("files", "persist", `Keeps ~/${value} inside the sandbox`, "low", { path: value });
    case "talk-name":
    case "own-name":
    case "see-name":
    case "call-name": {
      const verb = flag === "own-name" ? "own" : flag === "see-name" ? "see" : "talk";
      const verbLabel = verb === "own" ? "Owns" : verb === "see" ? "Sees" : "Talks to";
      if (ESCAPE_NAMES.has(value.replace(/\.\*$/, "")))
        return c("escape", `bus.${verb}`, `${verbLabel} ${value}`, "high", { name: value });
      if (value.startsWith("org.freedesktop.Notifications"))
        return c("notify", "bus.notifications", "Desktop notifications", "low");
      return c("bus", `bus.${verb}`, `${verbLabel} ${value}`, value.endsWith(".*") ? "medium" : "low", { name: value });
    }
    case "system-talk-name":
    case "system-own-name":
      return c("bus", "bus.system", `System bus: ${value}`, "medium", { name: value });
    case "allow": {
      const hit = ALLOW[value];
      if (hit) return c(hit[0], `allow.${value}`, hit[1], hit[2]);
      break;
    }
    case "env":
      return c("other", "env", `Sets ${value}`, "low", { name: value });
  }
  const negated = /^(unshare|nosocket|nodevice|disallow|nofilesystem)$/.test(flag);
  return negated
    ? c("other", "revokes", `Revokes ${value}`, "low", { name: value })
    : c("other", "raw", arg, "low", { arg });
}

/* With ``fallback-x11`` flatpak also records ``x11`` but only grants it
 * when no Wayland compositor runs: show the fallback, not both. */
function normalise(permissions: string[]): string[] {
  if (!permissions.includes("--socket=fallback-x11")) return permissions;
  return permissions.filter((p) => p !== "--socket=x11");
}

export function groupFlatpakPermissions(input: string[]): PermissionGroup[] {
  const permissions = normalise(input);
  const order = Object.keys(GROUPS);
  const map = new Map<string, PermissionItem[]>();
  for (const raw of permissions) {
    const cl = classify(raw);
    if (!map.has(cl.group)) map.set(cl.group, []);
    map.get(cl.group)!.push({ raw, label: cl.label, risk: cl.risk, key: cl.key, vars: cl.vars ?? {} });
  }
  const rank: Record<Risk, number> = { high: 0, medium: 1, low: 2 };
  return order
    .filter((k) => map.has(k))
    .map((k) => ({
      key: k,
      ...GROUPS[k],
      items: map.get(k)!.sort((a, b) => rank[a.risk] - rank[b.risk]),
    }));
}

export function overallRisk(input: string[]): Risk {
  let worst: Risk = "low";
  for (const p of normalise(input)) {
    const r = classify(p).risk;
    if (r === "high") return "high";
    if (r === "medium") worst = "medium";
  }
  return worst;
}
