'use client';

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useState, type ReactNode } from 'react';
import { useEventStream } from '@/lib/hooks/useEventStream';

function EventStreamProvider({ children }: { children: ReactNode }) {
  useEventStream();
  return <>{children}</>;
}

export function Providers({ children }: { children: ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 5000,
            refetchOnWindowFocus: false,
            retry: 1,
          },
        },
      }),
  );

  return (
    <QueryClientProvider client={queryClient}>
      <EventStreamProvider>{children}</EventStreamProvider>
    </QueryClientProvider>
  );
}
