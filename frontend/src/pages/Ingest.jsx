import React, { useEffect, useState } from "react";
import { api } from "../api.js";

export default function Ingest() {
  const [batches, setBatches] = useState([]);
  const [snaps, setSnaps] = useState([]);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const reload = () =>
    Promise.all([api("/api/batches"), api("/api/snapshots")])
      .then(([b, s]) => { setBatches(b); setSnaps(s); })
      .catch((e) => setErr(String(e.message || e)));

  useEffect(() => { reload(); }, []);

  const run = async () => {
    setErr("");
    try {
      const r = await api("/api/ingest/run", { method: "POST", body: "{}" });
      setMsg(JSON.stringify(r, null, 2));
      await reload();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  return (
    <>
      <div data-plant="25-fence-pass-stamp-miss" className="plant-mark" style={{display:"none"}} />
      <div className="topbar">
        <div>
          <h1>入档</h1>
          <p>把门店营业属性快照写入 source_snapshots（贴源入档批）。不在此页直接改历史维。</p>
        </div>
      </div>
      <div className="panel">
        <div className="actions">
          <button onClick={run}>跑变更夹具入档</button>
          <button className="secondary" onClick={reload}>刷新</button>
        </div>
        {msg && <pre className="msg">{msg}</pre>}
        {err && <p className="error">{err}</p>}
      </div>
      <div className="panel">
        <h2>入档批</h2>
        <table>
          <thead><tr><th>批号</th><th>时间</th><th>报告</th></tr></thead>
          <tbody>
            {batches.map((b) => (
              <tr key={b.id}>
                <td className="mono">{b.id}</td>
                <td>{b.created_at}</td>
                <td className="mono">{b.report_json}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="panel">
        <h2>快照（近 200）</h2>
        <table>
          <thead>
            <tr>
              <th>快照</th><th>门店</th><th>as_of</th><th>开-闭</th><th>mask</th><th>状态</th><th>入维</th><th>hash</th>
            </tr>
          </thead>
          <tbody>
            {snaps.map((s) => (
              <tr key={s.id}>
                <td className="mono">{s.id}</td>
                <td>{s.store_id}</td>
                <td>{s.as_of_ts}</td>
                <td>{s.open_time}–{s.close_time}</td>
                <td className="mono">{s.weekday_mask}</td>
                <td><span className={`badge ${s.biz_status}`}>{s.biz_status}</span></td>
                <td>
                  <span className={`badge ${s.applied === 0 ? "bad" : "ok"}`}>
                    {s.applied === 0 ? "旁路搁置" : "可入维"}
                  </span>
                </td>
                <td className="mono">{s.row_hash}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
