export function LoadingState({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 rounded-lg border border-base-hairline bg-base-raised/50 px-6 py-16 text-center">
      <div className="h-5 w-5 animate-spin rounded-full border-2 border-signal-teal/30 border-t-signal-teal" />
      <p className="font-mono text-xs text-ink-muted">{label}</p>
    </div>
  );
}

export function ErrorState({
  title = "Something went wrong",
  detail,
  onRetry,
}: {
  title?: string;
  detail?: string;
  onRetry?: () => void;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-lg border border-signal-rose/30 bg-signal-rose/[0.06] px-6 py-16 text-center">
      <p className="text-sm font-medium text-signal-rose">{title}</p>
      {detail && <p className="max-w-md font-mono text-xs text-ink-muted">{detail}</p>}
      {onRetry && (
        <button
          onClick={onRetry}
          className="mt-3 rounded-md border border-signal-rose/30 bg-signal-rose/10 px-3 py-1.5 text-xs font-medium text-signal-rose transition hover:bg-signal-rose/20"
        >
          Try again
        </button>
      )}
    </div>
  );
}

export function EmptyState({
  title,
  detail,
  action,
}: {
  title: string;
  detail?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-base-hairline px-6 py-16 text-center">
      <p className="text-sm font-medium text-ink">{title}</p>
      {detail && <p className="max-w-md text-xs text-ink-muted">{detail}</p>}
      {action && <div className="mt-3">{action}</div>}
    </div>
  );
}
