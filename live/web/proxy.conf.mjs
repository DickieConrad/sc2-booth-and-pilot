// ng serve proxy: every helper route goes to timing.py (SSE and /voice/ clips included).
// SC2_HELPER overrides the target (WSL in NAT mode cannot reach the Windows helper on 127.0.0.1; see README).
const target = process.env.SC2_HELPER || "http://127.0.0.1:8765";
const routes = ["/state", "/events", "/plans", "/notes", "/changelog", "/voice", "/history", "/vendor", "/say", "/note"];
export default Object.fromEntries(routes.map(r => [r, { target, secure: false, changeOrigin: false }]));
