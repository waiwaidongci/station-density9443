# gb-0042 验收脚本：误采清除

`purge_e2e.py` 使用临时 SQLite 与临时 artifacts 目录，不起容器；覆盖受理后封锁、冻结输入/结果/说明/文件去值、无关研究可用、幂等重试、中断续作、晚归 worker、旧批准说明服从清除、凭据不含原值。

```bash
cd /workspace
python3 test-artifacts/gb-0042/purge_e2e.py
```

依赖：`backend/requirements.txt` 及 `httpx`（TestClient）。脚本会自行创建账号与来源，不触碰演示库。
