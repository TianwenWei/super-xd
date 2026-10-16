import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', 'VITE_');
  const port = Number(env.VITE_PORT || 5173);
  const target = env.VITE_API_TARGET || 'http://127.0.0.1:8000';
  const api = new URL(target);
  if (!['http:', 'https:'].includes(api.protocol) || !Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid VITE_PORT or VITE_API_TARGET');
  return { plugins: [react()], server: { port, strictPort: true, proxy: { '/api': { target: api.origin }, '/ws': { target: api.origin, ws: true } } } };
});
