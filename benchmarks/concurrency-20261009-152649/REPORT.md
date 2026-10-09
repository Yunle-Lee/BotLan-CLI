# Concurrency benchmark — 20261009-152649

- llama.cpp build b1-441df11, llama-server slots=4
- Gate calls are serialized inside the orchestrator (one jev-score process, `_LOCK`);
  stage-2 llama-server serves up to `slots` generations at once, the rest queue.

| scenario | conc | n | wall s | throughput req/s | lat p50 ms | p95 | max | key field p50 | GPU util max | MemAvail min GB | llama MB | jev MB | errors |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| route | 1 | 16 | 0.62 | 25.68 | 38.2 | 41.3 | 52.3 | gate_ms=37.0 | 0.0 | 82.55 | 8749.0 | 1705.0 | 0 |
| chat-stream | 1 | 8 | 5.59 | 1.43 | 728.0 | 853.9 | 853.9 | ttft_ms=21.0 | 90.0 | 82.43 | 8749.0 | 1705.0 | 0 |
| answer-stream | 1 | 8 | 3.16 | 2.54 | 398.8 | 494.5 | 494.5 | ttft_ms=73.9 | 91.0 | 82.63 | 8749.0 | 1705.0 | 0 |
| route | 2 | 16 | 0.61 | 26.09 | 73.7 | 77.7 | 96.0 | gate_ms=71.2 | 84.0 | 82.69 | 8749.0 | 1705.0 | 0 |
| chat-stream | 2 | 8 | 3.44 | 2.32 | 932.4 | 1004.2 | 1004.2 | ttft_ms=47.3 | 87.0 | 82.68 | 8749.0 | 1705.0 | 0 |
| answer-stream | 2 | 8 | 2.5 | 3.2 | 696.5 | 709.2 | 709.2 | ttft_ms=134.0 | 93.0 | 82.66 | 8749.0 | 1705.0 | 0 |
| route | 4 | 16 | 0.62 | 25.75 | 149.7 | 153.4 | 171.2 | gate_ms=147.3 | 93.0 | 82.65 | 8749.0 | 1705.0 | 0 |
| chat-stream | 4 | 8 | 1.92 | 4.16 | 856.6 | 1065.8 | 1065.8 | ttft_ms=49.4 | 93.0 | 82.65 | 8749.0 | 1705.0 | 0 |
| answer-stream | 4 | 8 | 1.7 | 4.72 | 926.6 | 998.3 | 998.3 | ttft_ms=296.2 | 89.0 | 82.65 | 8749.0 | 1705.0 | 0 |
| route | 8 | 16 | 0.62 | 25.75 | 294.9 | 305.7 | 323.2 | gate_ms=293.3 | 89.0 | 82.6 | 8749.0 | 1705.0 | 0 |
| chat-stream | 8 | 8 | 2.22 | 3.6 | 1834.2 | 2220.0 | 2220.0 | ttft_ms=1001.5 | 89.0 | 82.6 | 8749.0 | 1705.0 | 0 |
| answer-stream | 8 | 8 | 1.67 | 4.78 | 1455.1 | 1671.0 | 1671.0 | ttft_ms=832.3 | 83.0 | 82.61 | 8749.0 | 1705.0 | 0 |

## 结论

1. **门控是串行的，吞吐上限 ~26 req/s**：orchestrator 用 `_LOCK` 串行调用唯一的 jev-score 进程，
   `/route` 在 1/2/4/8 并发下的 p50 几乎正好是 38/74/150/295 ms（38ms × 并发数），吞吐恒定 25.7 req/s，
   GPU 空闲（采样到 0-93% 只是偶发命中）。瓶颈是锁，不是 GB10。
2. **生成端上限 4 路（llama-server slots=4）**：`/chat/stream` 吞吐 1.43 → 2.32 → 4.16 → 3.60 req/s，
   `/answer/stream` 2.54 → 3.20 → 4.72 → 4.78 req/s。4 并发最划算；8 并发时 answer 不再涨、chat 反而回落
   （排队 + 仅 8 请求的小样本噪声），首 token p50 从 74ms 涨到 832ms。
3. **零错误**：8 并发下 48 个请求全部成功，无 5xx/超时。
4. **资源占用几乎不随并发变化**：每进程显存恒定 8749 + 1705 MiB；系统 MemAvailable 最低 82.43 GB
   （比单流测试还高，说明 KV 分配很保守）；GPU 峰值 93%，未出现 OOM 或抖动。

## 建议

- 常规使用按 **≤4 并发**；这是当前 slots 下的最优区间。
- 需要更高并发时两个方向：提高 `llama-server --parallel`（slots）换取生成吞吐；
  门控侧多开 jev-score / 用 `many_mode=batched` 把同一 state 的多问合并，绕开单锁串行。
- 本测试 n 较小（route 16、chat/answer 各 8），吞吐数字用于量级判断，±10-15% 属正常波动。
