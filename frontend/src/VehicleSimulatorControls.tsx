import {useEffect, useState} from 'react';
import './vehicle-simulator.css';

export default function VehicleSimulatorControls({vehicle, send}: {
  vehicle: Record<string, unknown>; send: (event: Record<string, unknown>) => void;
}) {
  const meta = (vehicle._meta || {}) as Record<string, unknown>;
  const navigation = (vehicle.navigation || {}) as Record<string, unknown>;
  const [speed, setSpeed] = useState(0);
  const [gear, setGear] = useState('P');
  const [call, setCall] = useState(false);
  const [navigating, setNavigating] = useState(false);
  const [zone, setZone] = useState(Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC');
  useEffect(() => {
    setSpeed(Number(meta.speed_kph || 0)); setGear(String(meta.gear || 'P'));
    setCall(meta.call_active === true); setNavigating(navigation.active === true);
    if (meta.timezone) setZone(String(meta.timezone));
  }, [meta.speed_kph, meta.gear, meta.call_active, meta.timezone, navigation.active]);
  return <form className="simulator-controls" onSubmit={event => {
    event.preventDefault();
    send({type:'vehicle.configure', settings:{speed_kph:speed, gear, call_active:call, navigation_active:navigating, timezone:zone}});
  }}>
    <strong>驾驶环境</strong>
    <label>车速 km/h<input aria-label="模拟车速" type="number" min="0" max="200" value={speed} onChange={e => setSpeed(Number(e.target.value))}/></label>
    <label>档位<select aria-label="模拟档位" value={gear} onChange={e => setGear(e.target.value)}>{['P','R','N','D'].map(item => <option key={item}>{item}</option>)}</select></label>
    <label><input type="checkbox" checked={navigating} onChange={e => setNavigating(e.target.checked)}/>导航中</label>
    <label><input type="checkbox" checked={call} onChange={e => setCall(e.target.checked)}/>通话中</label>
    <label>车辆时区<input aria-label="车辆时区" value={zone} onChange={e => setZone(e.target.value)} placeholder="Asia/Shanghai"/></label>
    <button type="button" onClick={() => setZone(Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC')}>使用设备时区</button>
    <button type="submit">应用驾驶环境</button>
    <small>模拟执行300ms + 网络往返400ms；P档车速为0。</small>
  </form>;
}
