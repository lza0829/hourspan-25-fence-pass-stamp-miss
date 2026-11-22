# 时档（HourSpan）

**骨架：** 门店营业属性贴源入档 → 业务键变更检测 → Type2 历史行开闭 → 当前镜投影 → 历史与当前互证

- 领域：数据工程（去皮=维表 SCD / 历史开闭 / 当前镜 / 核证；不是门店后台 CRUD）
- 端口：前端 **6200**，接口 **11200**（避开 Chrome 封锁的 6000–6063）
- 公有仓前缀：`lza0829/hourspan-NN-*`
- 栈：Python 3.12 + FastAPI + SQLite；前端 **React + Vite**
- 避让：勿撞筑层分层汇总、价舱改价调度、信汇多源去重、变流 CDC 游标、格湾占用态
- 序闸放行戳（`fence_pass_stamp`）全局唯一口径：**甲·以放行戳为后续写前置**
  - 合法序开段标成功时同事务落戳（`store_id / hist_id / open_time / close_time / passed_at`），与 hist 互证；缺戳 / 分叉即整笔回滚
  - 非法序与拒收路径无戳；当前段无有效戳则拒绝继续入档 / 检测 / 开段（即使 hist 已存在）
  - 只读核证：`GET /api/fence/pass-stamps`、`GET /api/fence/pass-stamps/verify`

本地冒烟：

```powershell
cd claude-workspace/hourspan/_scaffold/backend
pip install -r requirements.txt
$env:DATA_DIR="$PWD/data"; $env:FIXTURES_DIR="$PWD/../fixtures"
python -m uvicorn app.main:app --host 127.0.0.1 --port 11200

cd ../frontend
npm install
npm run dev
```

或：`docker compose up -d --build`（6200 / 11200）
