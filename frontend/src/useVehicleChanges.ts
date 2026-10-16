import { useEffect, useRef, useState } from 'react';
import { diffVehicleState, mergeVehicleChanges } from './vehicleChanges';
import type { VehicleChange } from './vehicleChanges';

export const VEHICLE_HIGHLIGHT_MS = 6000;

export function useVehicleChanges(vehicle: Record<string, unknown>) {
  const previous = useRef<Record<string, unknown> | undefined>(undefined);
  const [feedback, setFeedback] = useState({ sequence: 0, changes: new Map<string, VehicleChange>(), latestGroups: [] as string[] });
  useEffect(() => {
    const before = previous.current;
    previous.current = vehicle;
    // Initial snapshots establish a baseline; only later state changes are highlighted.
    if (!before || Object.keys(before).length === 0) return;
    const changes = diffVehicleState(before, vehicle);
    if (!changes.length) return;
    setFeedback(current => ({ sequence: current.sequence + 1,
      changes: mergeVehicleChanges(current.changes, changes, current.sequence + 1),
      latestGroups: [...new Set(changes.map(change => change.path[0]))] }));
  }, [vehicle]);

  useEffect(() => {
    if (!feedback.sequence) return;
    const sequence = feedback.sequence;
    const timer = window.setTimeout(() => setFeedback(current => current.sequence === sequence
      ? { ...current, changes: new Map(), latestGroups: [] } : current), VEHICLE_HIGHLIGHT_MS);
    return () => window.clearTimeout(timer);
  }, [feedback.sequence]);
  return feedback;
}
