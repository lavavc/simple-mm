import type { SystemStatus, VenueStatus } from '@/types';

type EngineStatusUpdate = Pick<SystemStatus, 'trading_enabled' | 'uptime'> & {
  venues: Pick<VenueStatus, 'name' | 'enabled' | 'paused'>[];
};

export function mergeEngineStatus(
  current: SystemStatus | undefined,
  update: EngineStatusUpdate,
): SystemStatus | undefined {
  if (!current) return current;
  const updates = new Map(update.venues.map((venue) => [venue.name, venue]));
  return {
    ...current,
    trading_enabled: update.trading_enabled,
    uptime: update.uptime,
    venues: current.venues.map((venue) => ({ ...venue, ...updates.get(venue.name) })),
  };
}
