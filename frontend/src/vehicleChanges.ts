export type VehicleChange = { path: string[]; before: unknown; after: unknown; sequence: number };

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function equalValue(before: unknown, after: unknown): boolean {
  if (Object.is(before, after)) return true;
  if (Array.isArray(before) && Array.isArray(after)) return before.length === after.length
    && before.every((item, index) => equalValue(item, after[index]));
  if (isRecord(before) && isRecord(after)) return Object.keys(before).length === Object.keys(after).length
    && Object.keys(before).every(key => Object.hasOwn(after, key) && equalValue(before[key], after[key]));
  return false;
}

export function vehiclePathKey(path: string[]): string { return JSON.stringify(path); }

export function diffVehicleState(before: unknown, after: unknown, path: string[] = []): Omit<VehicleChange, 'sequence'>[] {
  if (equalValue(before, after)) return [];
  if ((isRecord(before) || before == null) && (isRecord(after) || after == null)
      && (isRecord(before) || isRecord(after))) {
    const left = isRecord(before) ? before : {}, right = isRecord(after) ? after : {};
    return [...new Set([...Object.keys(left), ...Object.keys(right)])]
      .flatMap(key => diffVehicleState(left[key], right[key], [...path, key]));
  }
  return [{ path, before, after }];
}

export function mergeVehicleChanges(previous: Map<string, VehicleChange>, changes: Omit<VehicleChange, 'sequence'>[], sequence: number): Map<string, VehicleChange> {
  const merged = new Map(previous);
  for (const change of changes) merged.set(vehiclePathKey(change.path), { ...change, sequence });
  return merged;
}
