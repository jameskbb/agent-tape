/** The AgentTape mark: two tape reels joined by a strip of tape. */
export function ReelMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={className} aria-hidden>
      <rect width="32" height="32" rx="7" fill="var(--accent)" />
      <circle cx="11" cy="15" r="5.5" fill="none" stroke="var(--accent-ink)" strokeWidth="2.4" />
      <circle cx="21" cy="15" r="5.5" fill="none" stroke="var(--accent-ink)" strokeWidth="2.4" />
      <circle cx="11" cy="15" r="1.6" fill="var(--accent-ink)" />
      <circle cx="21" cy="15" r="1.6" fill="var(--accent-ink)" />
      <path d="M11 20.5h10" stroke="var(--live)" strokeWidth="2.4" strokeLinecap="round" />
    </svg>
  );
}
