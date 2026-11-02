import { CalendarClock } from "lucide-react";

import type { FeatureManifest } from "@/app/registry";

import { ScheduleDetailPage } from "./ScheduleDetailPage";
import { ScheduleFormPage } from "./ScheduleFormPage";
import { SchedulesPage } from "./SchedulesPage";

export const feature: FeatureManifest = {
  id: "schedules",
  nav: { label: "Schedules", icon: CalendarClock, order: 40, path: "/schedules" },
  routes: [
    { path: "schedules", element: <SchedulesPage /> },
    { path: "schedules/new", element: <ScheduleFormPage /> },
    { path: "schedules/:scheduleId", element: <ScheduleDetailPage /> },
    { path: "schedules/:scheduleId/edit", element: <ScheduleFormPage /> },
  ],
};
