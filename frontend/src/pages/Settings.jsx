import React, { useEffect, useState } from "react";
import { api } from "../api.js";

export default function Settings() {
  const [settings, setSettings] = useState({});
  const [stubs, setStubs] = useState([]);
  const [err, setErr] = useState("");

  useEffect(() => {
    Promise.all([api("/api/settings"), api("/api/stubs")])
      .then(([s, st]) => { setSettings(s); setStubs(st.stubs || []); })
      .catch((e) => setErr(String(e.message || e)));
  }, []);

  return (
    <>
      <div className="topbar">
        <div>
          <h1>站规</h1>
          <p>端口 6200 / 11200 · 公有仓 lza0829/hourspan-* · 禁门店运营 CRUD 皮。</p>
        </div>
      </div>
      {err && <p className="error">{err}</p>}
      <div className="panel">
        <h2>设置</h2>
        <table>
          <thead><tr><th>键</th><th>值</th></tr></thead>
          <tbody>
            {Object.entries(settings).map(([k, v]) => (
              <tr key={k}><td>{k}</td><td className="mono">{v}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="panel">
        <h2>空桩清单</h2>
        <ul>
          {stubs.map((s) => (
            <li key={s} className="mono">{s}</li>
          ))}
        </ul>
      </div>
    </>
  );
}
