/**
 * Daily run outcomes as stacked bars. Status colours carry meaning (succeeded / failed),
 * so they come from the reserved chart status tokens; failed segments are also hatched
 * so the pair never relies on colour alone. A legend, a hover tooltip, and a table view
 * (in OverviewPage) carry the same numbers as text.
 */
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { useTheme } from "@/app/theme";

import type { DayActivity } from "./api";

interface Palette {
  good: string;
  bad: string;
  other: string;
  grid: string;
  muted: string;
  surface: string;
}

/** Mirrors the --chart-* tokens in styles/index.css (SVG attributes need concrete values). */
const PALETTES: Record<"light" | "dark", Palette> = {
  light: {
    good: "#1d7a4b",
    bad: "#c0322b",
    other: "#8494a6",
    grid: "#e3e8ee",
    muted: "#5b6b7d",
    surface: "#ffffff",
  },
  dark: {
    good: "#1c7652",
    bad: "#e2725c",
    other: "#66788d",
    grid: "#22324a",
    muted: "#8d9db1",
    surface: "#142030",
  },
};

export function formatDay(date: string, style: "short" | "long" = "short"): string {
  const [y, m, d] = date.split("-").map(Number);
  const value = new Date(Date.UTC(y ?? 1970, (m ?? 1) - 1, d ?? 1));
  return value.toLocaleDateString(
    undefined,
    style === "short"
      ? { month: "short", day: "numeric", timeZone: "UTC" }
      : { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" },
  );
}

interface TooltipContent {
  active?: boolean;
  payload?: { payload?: DayActivity }[];
  label?: string | number;
}

function ChartTooltip({ active, payload, label }: TooltipContent) {
  if (!active || !payload?.length || typeof label !== "string") return null;
  const row = payload[0]?.payload;
  if (!row) return null;
  return (
    <div className="rounded-[var(--radius-control)] border border-line bg-raised px-3 py-2 text-sm shadow-[var(--shadow-pop)]">
      <p className="font-medium text-ink">{formatDay(label, "long")}</p>
      <dl className="mt-1 grid grid-cols-[auto_auto] gap-x-4 text-ink-soft tabular-nums">
        <dt>Succeeded</dt>
        <dd className="text-right">{row.succeeded}</dd>
        <dt>Failed or timed out</dt>
        <dd className="text-right">{row.failed}</dd>
        <dt>Cancelled or skipped</dt>
        <dd className="text-right">{row.other}</dd>
      </dl>
    </div>
  );
}

export default function ActivityChart({ data }: { data: DayActivity[] }) {
  const { resolved } = useTheme();
  const palette = PALETTES[resolved];
  const max = Math.max(1, ...data.map((d) => d.succeeded + d.failed + d.other));
  return (
    <div
      className="h-64 w-full"
      role="img"
      aria-label="Runs per day by outcome; the table view lists the same numbers"
    >
      <ResponsiveContainer width="100%" height="100%">
        <BarChart
          data={data}
          margin={{ top: 8, right: 8, bottom: 0, left: -12 }}
          barCategoryGap="22%"
        >
          <defs>
            <pattern
              id="at-hatch"
              patternUnits="userSpaceOnUse"
              width="6"
              height="6"
              patternTransform="rotate(45)"
            >
              <rect width="6" height="6" fill={palette.bad} />
              <line
                x1="0"
                y1="0"
                x2="0"
                y2="6"
                stroke={palette.surface}
                strokeWidth="2"
                strokeOpacity="0.55"
              />
            </pattern>
          </defs>
          <CartesianGrid vertical={false} stroke={palette.grid} />
          <XAxis
            dataKey="date"
            tickFormatter={(d: string) => formatDay(d)}
            tick={{ fill: palette.muted, fontSize: 12 }}
            tickLine={false}
            axisLine={{ stroke: palette.grid }}
            interval="preserveStartEnd"
            minTickGap={16}
          />
          <YAxis
            allowDecimals={false}
            domain={[0, max]}
            tick={{ fill: palette.muted, fontSize: 12 }}
            tickLine={false}
            axisLine={false}
            width={40}
          />
          <Tooltip content={<ChartTooltip />} cursor={{ fill: palette.grid, fillOpacity: 0.5 }} />
          <Bar
            dataKey="succeeded"
            stackId="runs"
            fill={palette.good}
            stroke={palette.surface}
            strokeWidth={1}
            isAnimationActive={false}
          />
          <Bar
            dataKey="failed"
            stackId="runs"
            fill="url(#at-hatch)"
            stroke={palette.surface}
            strokeWidth={1}
            isAnimationActive={false}
          />
          <Bar
            dataKey="other"
            stackId="runs"
            fill={palette.other}
            stroke={palette.surface}
            strokeWidth={1}
            radius={[3, 3, 0, 0]}
            isAnimationActive={false}
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
