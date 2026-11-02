import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { createBrowserRouter, RouterProvider, type RouteObject } from "react-router";

import { ApiError } from "@/api/client";
import { ToastProvider } from "@/components/ui";

import { NotFound } from "./NotFound";
import { RootError, RouteError } from "./RouteError";
import { features } from "./registry";
import { Shell } from "./Shell";
import { ThemeProvider } from "./theme";

export function buildRoutes(): RouteObject[] {
  return [
    {
      path: "/",
      element: <Shell />,
      errorElement: <RootError />,
      children: [
        {
          // Pathless layout route: a page that throws renders RouteError inside the Shell.
          errorElement: <RouteError />,
          children: [...features.flatMap((f) => f.routes), { path: "*", element: <NotFound /> }],
        },
      ],
    },
  ];
}

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 2_000,
        refetchOnWindowFocus: true,
        retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
      },
    },
  });
}

export function App() {
  const [client] = useState(makeQueryClient);
  const [router] = useState(() => createBrowserRouter(buildRoutes()));
  return (
    <ThemeProvider>
      <QueryClientProvider client={client}>
        <ToastProvider>
          <RouterProvider router={router} />
        </ToastProvider>
      </QueryClientProvider>
    </ThemeProvider>
  );
}
