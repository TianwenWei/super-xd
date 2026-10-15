import type { VehicleFunctionCall } from './types';
import { vehicleCallStatus } from './vehicleCallViews';

function formatFunctionCall(name: string, rawArguments: string): string {
  try { return `${name}(${JSON.stringify(JSON.parse(rawArguments), null, 2)})`; }
  catch { return `${name}(${rawArguments})`; }
}

export default function VehicleCalls({ calls }: { calls: VehicleFunctionCall[] }) {
  return <div className="vehicle-function-calls" aria-label="生成的车控 function call"><span>车控 function call</span>{calls.map(call => <pre key={call.id}>{call.source === 'vehicle_plan' && <span>Pro规划 · {vehicleCallStatus(call)}{'\n'}</span>}<code>{formatFunctionCall(call.function.name, call.function.arguments)}</code></pre>)}</div>;
}
