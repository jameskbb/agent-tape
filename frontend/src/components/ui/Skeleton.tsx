import { cn } from "@/lib/cn";

export function Skeleton({ className, lines = 1 }: { className?: string; lines?: number }) {
  return (
    <div aria-hidden className="flex flex-col gap-2" data-skeleton="">
      {Array.from({ length: lines }, (_, i) => (
        <div
          key={i}
          className={cn(
            "at-skeleton h-4 rounded",
            i === lines - 1 && lines > 1 && "w-2/3",
            className,
          )}
        />
      ))}
    </div>
  );
}

export function LoadingBlock({ label = "Loading" }: { label?: string }) {
  return (
    <div role="status" aria-live="polite" className="flex flex-col gap-3 py-2">
      <span className="sr-only">{label}</span>
      <Skeleton lines={3} />
    </div>
  );
}
