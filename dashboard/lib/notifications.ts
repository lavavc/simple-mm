import { useSyncExternalStore } from 'react';

export type Notification = {
  id: string;
  type: 'arbitrage' | 'alert' | 'info';
  title: string;
  message: string;
  timestamp: number;
  data?: Record<string, unknown>;
};

const MAX_VISIBLE = 5;
const AUTO_DISMISS_MS = 8000;

let notifications: Notification[] = [];
let listeners: Array<() => void> = [];

function emitChange() {
  for (const listener of listeners) {
    listener();
  }
}

export function addNotification(
  notification: Omit<Notification, 'id' | 'timestamp'>,
) {
  const id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const entry: Notification = {
    ...notification,
    id,
    timestamp: Date.now(),
  };

  notifications = [entry, ...notifications].slice(0, MAX_VISIBLE);
  emitChange();

  setTimeout(() => {
    dismissNotification(id);
  }, AUTO_DISMISS_MS);
}

export function dismissNotification(id: string) {
  const prev = notifications;
  notifications = notifications.filter((n) => n.id !== id);
  if (notifications !== prev) {
    emitChange();
  }
}

function subscribe(listener: () => void) {
  listeners = [...listeners, listener];
  return () => {
    listeners = listeners.filter((l) => l !== listener);
  };
}

function getSnapshot() {
  return notifications;
}

export function useNotifications() {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
