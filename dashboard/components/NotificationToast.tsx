'use client';

import { useNotifications, dismissNotification } from '@/lib/notifications';
import { ArrowRightLeft, X } from 'lucide-react';

export function NotificationToast() {
  const notifications = useNotifications();

  if (notifications.length === 0) return null;

  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col-reverse gap-2 max-w-sm">
      {notifications.map((n) => {
        const isArb = n.type === 'arbitrage';
        const borderColor = isArb ? 'border-green-500/50' : 'border-yellow-500/50';

        return (
          <div
            key={n.id}
            className={`animate-toast-in rounded-lg border ${borderColor} bg-card text-card-foreground shadow-lg overflow-hidden`}
          >
            <div className="flex items-start gap-3 p-3">
              <div className={`mt-0.5 rounded-md p-1.5 ${isArb ? 'bg-green-500/10 text-green-500' : 'bg-yellow-500/10 text-yellow-500'}`}>
                <ArrowRightLeft className="h-4 w-4" />
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium leading-tight">
                  {n.title}
                </p>
                <p className="text-xs text-muted-foreground mt-0.5">
                  {n.message}
                </p>
              </div>
              <button
                onClick={() => dismissNotification(n.id)}
                className="text-muted-foreground hover:text-foreground transition-colors"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="h-0.5 bg-muted">
              <div
                className={`h-full ${isArb ? 'bg-green-500/40' : 'bg-yellow-500/40'} animate-toast-progress`}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}
