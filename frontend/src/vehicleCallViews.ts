import type { VehicleFunctionCall, VehiclePlanView } from './types';

export function mergeVehicleCalls(...groups: Array<VehicleFunctionCall[] | undefined>): VehicleFunctionCall[] {
  const calls = new Map<string, VehicleFunctionCall>();
  for (const group of groups) for (const call of group || []) {
    const old = calls.get(call.id);
    calls.set(call.id, { ...old, ...call, receipt: call.receipt ?? old?.receipt });
  }
  return [...calls.values()];
}

// View-only reconstruction. These actions are never inserted into model messages.
export function snapshotVehicleCalls(
  turn: { vehicle_calls?: VehicleFunctionCall[]; vehicle_plans?: VehiclePlanView[] },
  existing: VehicleFunctionCall[] = [],
): VehicleFunctionCall[] {
  const planned = (turn.vehicle_plans || []).flatMap(execution => {
    const receipts = execution.action_receipts ?? execution.ledger?.action_receipts ?? [];
    return (execution.plan?.actions || []).map(action => {
      const receipt = receipts.find(item => item.action_id === action.id);
      return {
        id: `${execution.parent_call_id}:${action.id}`,
        function: { name: action.name, arguments: JSON.stringify(action.arguments) },
        source: 'vehicle_plan', parent_call_id: execution.parent_call_id,
        action_id: action.id, intent_id: action.intent_id,
        ...(receipt ? { receipt, command_id: receipt.command_id } : {}),
      };
    });
  });
  return mergeVehicleCalls(existing, turn.vehicle_calls, planned);
}

export function liveVehicleCall(event: { call: VehicleFunctionCall; source?: string; parent_call_id?: string; action_id?: string; intent_id?: string }): VehicleFunctionCall {
  const call = { ...event.call };
  for (const key of ['source', 'parent_call_id', 'action_id', 'intent_id'] as const) {
    if (event[key] !== undefined) call[key] = event[key];
  }
  return call;
}

export function bindVehicleCommand(calls: VehicleFunctionCall[] | undefined, command: { command_id: string; parent_call_id?: string; action_id?: string }): VehicleFunctionCall[] {
  return (calls || []).map(call => call.source === 'vehicle_plan' && call.parent_call_id === command.parent_call_id && call.action_id === command.action_id
    ? { ...call, command_id: command.command_id } : call);
}

export function applyVehicleReceipt(calls: VehicleFunctionCall[] | undefined, commandId: string, result: Record<string, unknown>): VehicleFunctionCall[] {
  return (calls || []).map(call => call.command_id === commandId && call.action_id
    ? { ...call, receipt: { action_id: call.action_id, command_id: commandId, status: result.ok === true ? 'completed' : 'failed', result } } : call);
}

export function vehicleCallStatus(call: VehicleFunctionCall): string {
  if (!call.receipt) return '已规划 / 待回执';
  if (call.receipt.status === 'completed' && call.receipt.result?.ok === true) return '回执成功';
  return ({ failed: '回执失败', blocked: '依赖阻断 / 未执行', interrupted: '已打断 / 未执行' } as Record<string, string>)[call.receipt.status] || '回执未确认';
}
