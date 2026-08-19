import { toneClasses, type Tone } from "@/lib/format";
import clsx from "clsx";

export function StatusBadge({
  label,
  tone,
  pulse = false,
}: {
  label: string;
  tone: Tone;
  pulse?: boolean;
}) {
  const c = toneClasses(tone);
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 font-mono text-[11px] uppercase tracking-wide",
        c.bg,
        c.text,
        c.border,
      )}
    >
      <span className={clsx("h-1.5 w-1.5 rounded-full", c.dot, pulse && "animate-pulseDot")} />
      {label}
    </span>
  );
}
