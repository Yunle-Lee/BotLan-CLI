# jevstep / Spark Duo benchmark report — 2026-10-09 15:23（修正版）

## 环境
- GPU: NVIDIA GB10, 580.126.09, 12.1, [N/A]
- 内核: Linux spark-8691 6.17.0-1008-nvidia #8-Ubuntu SMP PREEMPT_DYNAMIC Wed Jan 21 17:56:56 UTC 2026 aarch64 aarch64 aarch64 GNU/Linux
- CUDA: /usr/local/cuda-13.0（sm_121 原生 SASS）
- llama.cpp: b1-441df11 · n_ctx=32768 · slots=4
- 进程: llama-server 1080075 · orchestrator 1592695 · jev-score 1592720

## 方法
- 全部走运行中的 HTTP 服务（/route /decide /answer /answer/stream /chat/stream /agent），即 jevstep 的真实路径
- 每场景暖机后测；资源采样 0.5s 一次（nvidia-smi + /proc/meminfo）
- 长上下文：不同 state 测冷 prefill，同一 state 测 KV 前缀复用；answer-short/stream 为热缓存

## 结果

| 场景 | 输入 tokens | n | wall p50 | wall p95 | 关键指标 p50 | GPU util max/mean | 功耗 max | 温度 max | MemAvail min | llama-server MB | jev-score MB |
|---|---|---|---|---|---|---|---|---|---|---|---|
| gate-short | 35 | 20/20 | 37.6 ms | 40.1 ms | gate_ms=36.5  | 85.0/85.0 | 24.95 W | 54.0 °C | 82.9 GB | 8749.0 | 1705.0 |
| gate-long-cold | 17369 | 3/3 | 1457.7 ms | 1458.8 ms | gate_ms=1453.4  | 95.0/95.0 | 72.21 W | 58.0 °C | 82.88 GB | 8749.0 | 1705.0 |
| gate-long-warm | 21273 | 5/5 | 71.9 ms | 88.8 ms | gate_ms=70.4  | 95.0/95.0 | 73.39 W | 60.0 °C | 82.84 GB | 8749.0 | 1705.0 |
| decide-short | 35 | 20/20 | 8.1 ms | 8.4 ms | ms=7.2  | 48.0/48.0 | 68.39 W | 59.0 °C | 82.85 GB | 8749.0 | 1705.0 |
| answer-short | 35 | 10/10 | 681.9 ms | 711.9 ms | total_ms=680.6  | 91.0/90.0 | 54.22 W | 60.0 °C | 82.81 GB | 8749.0 | 1705.0 |
| answer-long | 11569 | 3/3 | 6998.5 ms | 7005.1 ms | total_ms=6994.8  | 96.0/90.0 | 86.44 W | 70.0 °C | 82.74 GB | 8749.0 | 1705.0 |
| answer-stream-short | 35 | 10/10 | 669.9 ms | 708.1 ms | total_ms=638.4 （TTFT 0.071 s, decode 74.094 tok/s） | 91.0/90.0 | 47.95 W | 66.0 °C | 82.27 GB | 8749.0 | 1705.0 |
| chat-stream | 25 | 5/5 | 1188.4 ms | 1359.6 ms | ttft_s=0.022 （TTFT 0.022 s, decode 74.512 tok/s） | 91.0/90.7 | 46.27 W | 66.0 °C | 82.32 GB | 8749.0 | 1705.0 |
| vlm-raw | 25 | 3/3 | 1004.1 ms | 1134.4 ms | wall_s=1.037 （decode 74.448 tok/s） | 90.0/90.0 | 46.33 W | 66.0 °C | 82.47 GB | 8749.0 | 1705.0 |
| agent-read | – | 3/3 | 2140.3 ms | 2141.8 ms | total_ms=2139.1  | 91.0/89.5 | 48.49 W | 68.0 °C | 82.74 GB | 8749.0 | 1705.0 |

## 资源占用（关键）
- 空闲基线（两模型常驻）: llama-server 8749 MiB + jev-score 1705 MiB = 10454 MiB；MemAvailable 82.87 GB；GPU 0%，12.6 W，48 °C
- 峰值: GPU 96%，功耗 86.4 W（长上下文 answer），温度 70 °C；MemAvailable 最低 82.23 GB（比空闲只少 ~0.6 GB）
- 每进程显存全程恒定：权重常驻；GB10 统一内存下 nvidia-smi 的 device total 是 N/A

## 关键数字
- 门控: 短 state 37 ms p50；17369 token 冷 prefill 1.45 s（≈12k tok/s）；同 state 复用 70 ms
- Jev 裁决: 8 ms；pipeline 短问: 682 ms（热缓存：gate 51 + vlm 600 + verify 30）
- pipeline 长文（11569 token）: 7.0 s（含 gate 与 verify 两次长 prefill）
- 流式: 首 token 71 ms（pipeline）/ 22 ms（direct）；decode 74 tok/s
- agent 读文件一步: 2.14 s

## 注意
- 0.5 s 采样会漏掉 40 ms 级操作的 GPU 峰值，gate/decide 的 util 仅供参考
- answer-short 为热缓存；冷态见 answer-long
- llama-server slots=4，并发上限参考
- 原始数据: raw.json；聚合: summary.json；脚本: bench.py（同目录）
