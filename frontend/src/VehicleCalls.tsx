import type { VehicleFunctionCall } from './types';
import { vehicleCallStatus } from './vehicleCallViews';

function formatFunctionCall(name: string, rawArguments: string): string {
  try { const args = JSON.parse(rawArguments); const compact = JSON.stringify(args); return `${name}(${compact.length <= 90 ? compact : JSON.stringify(args, null, 2)})`; }
  catch { return `${name}(${rawArguments})`; }
}

export default function VehicleCalls({ calls }: { calls: VehicleFunctionCall[] }) {
  return <div className="vehicle-function-calls" aria-label="生成的车控指令"><span>车控指令</span>{calls.map(call => <pre key={call.id}>{call.source === 'vehicle_plan' && <span>规划 · {vehicleCallStatus(call)}{'\n'}</span>}<code className="language-javascript">{formatFunctionCall(call.function.name, call.function.arguments)}</code></pre>)}</div>;
}
