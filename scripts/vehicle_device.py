"""Drive the same delayed simulator endpoint as the browser, without fake ACKs."""
import json


async def execute_vehicle(client, base, session_id, command, socket):
    response = await client.post(f"{base.rstrip('/')}/api/sessions/{session_id}/vehicle/commands/{command['command_id']}/execute")
    response.raise_for_status()
    receipt = response.json()
    await socket.send(json.dumps({'type':'vehicle.receipt', 'command_id':receipt['command_id'],
                                  'receipt_id':receipt['receipt_id']}))
