# jevstep benchmark — 20261009-152311

- GPU: NVIDIA GB10, 580.126.09, 12.1, [N/A]
- llama.cpp: b1-441df11, n_ctx=32768, slots=4
- idle: {"gpu_util_max": 0.0, "gpu_util_mean": 0.0, "power_max_w": 12.59, "temp_max_c": 48.0, "mem_available_min_gb": 82.87, "samples": 2, "llama-server_gpu_mib_max": 8749.0, "jev-score_gpu_mib_max": 1705.0, "gpu_mib_total_max": 10454.0}

| scenario | n | wall p50 ms | wall p95 ms | key metric | GPU util max/mean % | mem_avail min GB | llama-server MiB | jev-score MiB |
|---|---|---|---|---|---|---|---|---|
| gate-short | 20/20 | 37.6 | 40.1 | gate_ms p50=36.5 | 85.0/20.0 | 82.89 | 8749.0 | 1705.0 |
| gate-long-cold | 3/3 | 1457.7 | 1458.8 | gate_ms p50=1453.4 | 95.0/92.767 | 82.85 | 8749.0 | 1705.0 |
| gate-long-warm | 5/5 | 71.9 | 88.8 | gate_ms p50=70.4 | 95.0/95.0 | 82.84 | 8749.0 | 1705.0 |
| decide-short | 20/20 | 8.1 | 8.4 | ms p50=7.2 | 48.0/48.0 | 82.85 | 8749.0 | 1705.0 |
| answer-short | 10/10 | 681.9 | 711.9 | total_ms p50=680.6 | 91.0/86.2 | 82.75 | 8749.0 | 1705.0 |
| answer-long | 3/3 | 6998.5 | 7005.1 | total_ms p50=6994.8 | 96.0/86.6 | 82.64 | 8749.0 | 1705.0 |
| answer-stream-short | 10/10 | 669.9 | 708.1 | total_ms p50=638.4, decode p50=74.094 tok/s | 91.0/78.3 | 82.23 | 8749.0 | 1705.0 |
| chat-stream | 5/5 | 1188.4 | 1359.6 | ttft_s p50=0.022, decode p50=74.512 tok/s | 91.0/89.96 | 82.27 | 8749.0 | 1705.0 |
| vlm-raw | 3/3 | 1004.1 | 1134.4 | , decode p50=74.448 tok/s | 90.0/89.433 | 82.41 | 8749.0 | 1705.0 |
| agent-read | 3/3 | 2140.3 | 2141.8 | total_ms p50=2139.1 | 91.0/88.633 | 82.72 | 8749.0 | 1705.0 |
