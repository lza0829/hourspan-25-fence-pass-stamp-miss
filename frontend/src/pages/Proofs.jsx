import React, { useEffect, useState } from "react";
import { api } from "../api.js";

export default function Proofs() {
  const [rows, setRows] = useState([]);
  const [companion, setCompanion] = useState(null);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const reload = () => {
    api("/api/proofs").then(setRows).catch((e) => setErr(String(e.message || e)));
    Promise.resolve(api("/api/fence/pass-stamps")).then(setCompanion).catch(() => setCompanion(null));
  };

  useEffect(() => { reload(); }, []);

  const rebuild = async () => {
    setErr("");
    try {
      const r = await api("/api/proofs/rebuild", { method: "POST", body: "{}" });
      setMsg(JSON.stringify(r, null, 2));
      await reload();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  return (
    <>
      <div className="topbar">
        <div>
          <h1>核证</h1>
          <p>当前镜 hist_id / row_hash 必须与 is_current 历史行一致；失败须可观测。</p>
        </div>
      </div>
      <div className="panel">
        <div className="actions">
          <button onClick={rebuild}>重建核证</button>
          <button className="secondary" onClick={reload}>刷新</button>
        </div>
        {msg && <pre className="msg">{msg}</pre>}
        {err && <p className="error">{err}</p>}
      </div>
      
      <div className="panel">
        <h2>序闸放行戳核证</h2>
        {companion ? (
          <>
            <p className="muted">
              {companion.architecture_label || ""} · 戳 {companion.pass_n} 条 ·
              结论 <b>{companion.stamp_ready ? "互证通过" : "存在问题"}</b>
            </p>
            <p className="mono">{companion.rule || ""}</p>
            <table>
              <thead>
                <tr>
                  <th>门店</th><th>当前 hist</th><th>戳</th><th>hist 存在</th><th>后续写</th><th>原因</th>
                </tr>
              </thead>
              <tbody>
                {(companion.verification?.segments || []).map((v) => (
                  <tr key={v.store_id}>
                    <td>{v.store_id}</td>
                    <td className="mono">{v.hist_id || "—"}</td>
                    <td>
                      <span className={`badge ${v.stamp_present ? "ok" : "bad"}`}>
                        {v.stamp_present ? "有" : "缺"}
                      </span>
                    </td>
                    <td>{v.hist_exists ? "是" : "否"}</td>
                    <td>
                      <span className={`badge ${v.subsequent_write === "allowed" ? "ok" : "bad"}`}>
                        {v.subsequent_write === "allowed" ? "放行" : "拒绝"}
                      </span>
                    </td>
                    <td className="mono">{v.reason || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <pre className="msg">{JSON.stringify(companion.verification || {}, null, 2)}</pre>
          </>
        ) : (
          <pre className="msg">（空）</pre>
        )}
      </div>
      <div className="panel">
        <h2>scd_proofs</h2>
        <table>
          <thead>
            <tr>
              <th>id</th><th>门店</th><th>hist</th><th>mirror_hist</th><th>hash</th><th>valid</th><th>detail</th><th>时间</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{r.id}</td>
                <td>{r.store_id}</td>
                <td className="mono">{r.hist_id}</td>
                <td className="mono">{r.mirror_hist_id}</td>
                <td className="mono">{r.row_hash}</td>
                <td>
                  <span className={`badge ${r.valid ? "ok" : "bad"}`}>
                    {r.valid ? "通过" : "失败"}
                  </span>
                </td>
                <td className="mono">{r.detail_json}</td>
                <td>{r.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
