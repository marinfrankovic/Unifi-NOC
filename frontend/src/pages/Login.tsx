import { FormEvent, useState } from "react";
import { api } from "../api";
import { SetupShell } from "../components/SetupShell";
import { Brand } from "../components/ui";

export default function Login({ onLogin }: { onLogin: () => void }) {
  const [username, setU] = useState(() => localStorage.getItem("noc_username") || "");
  const [password, setP] = useState("");
  const [rememberMe, setRememberMe] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      await api("/auth/login", { method: "POST", body: JSON.stringify({ username, password, remember_me: rememberMe }) });
      localStorage.setItem("noc_username", username);
      setP("");
      onLogin();
    } catch (ex: any) {
      setErr(ex?.message || "Login failed");
    } finally {
      setBusy(false);
    }
  };
  return (
    <SetupShell compact>
      <form className="card login-form" method="post" autoComplete="on" onSubmit={submit}>
        <Brand subtitle="Sign in to continue" />
        <label htmlFor="login-username">Username<input id="login-username" name="username" autoFocus autoComplete="username" required value={username} onChange={(e) => setU(e.target.value)} /></label>
        <label htmlFor="login-password">Password<input id="login-password" name="password" type="password" autoComplete="current-password" required value={password} onChange={(e) => setP(e.target.value)} /></label>
        <label className="row login-remember"><input name="remember_me" type="checkbox" checked={rememberMe} onChange={event => setRememberMe(event.target.checked)} />Remember me for 30 days</label>
        {err && <div className="err" role="alert">{err}</div>}
        <button type="submit" className="primary" disabled={busy || !username || !password}>{busy ? "Signing in…" : "Sign in"}</button>
      </form>
    </SetupShell>
  );
}
