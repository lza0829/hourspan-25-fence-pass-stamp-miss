import React, { useEffect, useState } from "react";
import { api } from "../api.js";

export default function Fence() {
  const [board, setBoard] = useState(null);
  const [err, setErr] = useState("");

  const reload = () =>
    api("/api/fence/board").then(setBoard).catch((e) => setErr(String(e.message || e)));

  useEffect(() => { reload(); }, []);

  const counts = board?.["计数"] || {};
  const cards = [
    ["搁置总数", counts["搁置总数"]],
    ["旁路透照(applied=0)", counts["旁路透照"]],
    ["未提交草稿", counts["未提交草稿"]],
    ["已提交草稿", counts["已提交草稿"]],
    ["已废弃草稿", counts["已废弃草稿"]],
    ["审计条", counts["审计条"]],
  ];

  return (
    <>
      <div data-plant="25-fence-pass-stamp-miss" className="plant-mark" style={{display:"none"}} />
      <div className="topbar">
        <div>
          <h1>序栅栏</h1>
          <p>
            close_time 与 open_time 均非空时须 <b>close_time &gt; open_time</b>；
            非法序快照 applied=0 旁路 + 搁置（留载荷/原因/关联 run/草稿清理位），
            不进历史维与当前镜。
          </p>
          <p className="muted mono">
            落库甲：open_close_quarantine · 失败闭甲：入口分流清本键未提交草稿
          </p>
        </div>
      </div>
      {err && <p className="error">{err}</p>}
      <div className="grid-cards">
        {cards.map(([k, v]) => (
          <div className="card" key={k}>
            <div className="label">{k}</div>
            <div className="value">{v ?? "-"}</div>
          </div>
        ))}
      </div>

      <div className="panel">
        <div className="actions">
          <h2 style={{ margin: 0 }}>搁置队列 open_close_quarantine</h2>
          <button className="secondary" onClick={reload}>刷新</button>
        </div>
        <table>
          <thead>
            <tr>
              <th>id</th><th>快照 id</th><th>门店</th><th>原因</th>
              <th>关联 run</th><th>草稿清理位</th><th>清理的草稿</th><th>载荷</th><th>时间</th>
            </tr>
          </thead>
          <tbody>
            {(board?.["搁置队列"] || []).map((r) => (
              <tr key={r.id}>
                <td>{r.id}</td>
                <td className="mono">{r.snapshot_id}</td>
                <td>{r.store_id}</td>
                <td><span className="badge bad">{r.reason}</span></td>
                <td className="mono">{r.related_run_id || "—"}</td>
                <td>
                  <span className={`badge ${r.draft_cleared ? "bad" : "ok"}`}>
                    {r.draft_cleared ? "已清理" : "无草稿"}
                  </span>
                </td>
                <td className="mono">{r.cleared_draft_ids_json}</td>
                <td className="mono">{(r.payload_json || "").slice(0, 80)}</td>
                <td>{r.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h2>Type2 草稿 scd_drafts（未提交=open / 已提交=applied / 废弃=discarded）</h2>
        <table>
          <thead>
            <tr>
              <th>草稿</th><th>检测跑次</th><th>门店</th><th>快照</th>
              <th>kind</th><th>状态</th><th>创建</th><th>应用时间</th>
            </tr>
          </thead>
          <tbody>
            {(board?.["草稿"] || []).map((r) => (
              <tr key={r.id}>
                <td className="mono">{r.id}</td>
                <td className="mono">{r.detect_run_id}</td>
                <td>{r.store_id}</td>
                <td className="mono">{r.snapshot_id}</td>
                <td>{r.kind}</td>
                <td>
                  <span
                    className={`badge ${
                      r.status === "applied" ? "ok" : r.status === "discarded" ? "bad" : "suspended"
                    }`}
                  >
                    {r.status}
                  </span>
                </td>
                <td>{r.created_at}</td>
                <td>{r.applied_at || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h2>驳回审计流 fence_proofs（只追加）</h2>
        <table>
          <thead>
            <tr>
              <th>id</th><th>快照</th><th>门店</th><th>事件</th>
              <th>原因</th><th>关联 run</th><th>草稿清理位</th><th>明细</th><th>时间</th>
            </tr>
          </thead>
          <tbody>
            {(board?.["核证流"] || []).map((r) => (
              <tr key={r.id}>
                <td>{r.id}</td>
                <td className="mono">{r.snapshot_id}</td>
                <td>{r.store_id}</td>
                <td><span className="badge suspended">{r.event}</span></td>
                <td>{r.reason || "—"}</td>
                <td className="mono">{r.related_run_id || "—"}</td>
                <td>{r.draft_cleared ? "是" : "否"}</td>
                <td className="mono">{(r.detail_json || "").slice(0, 80)}</td>
                <td>{r.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
