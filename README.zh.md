# Spark Duo — DGX Spark 上的 Jev 门控 + StepFun VLM

一个两阶段快速应答系统：让**两个**模型同时常驻 GB10 的一致性统一内存 —— 一个 0.53 GB 的决策模型决定做什么，一个 4B 视觉语言模型负责回答。

[English](README.md) | **中文**

> **DGX Spark 上的 BotLan CLI。** BotLan 的前端（本仓库）驱动的正是这套栈：`jevstep` CLI 和 agent 窗口与 Jev + GELab-Zero-4B 对话，二者都本地运行在 GB10 上。安装方式、基准数据和全部实测表格都在下文。

```
request（state 最长 25,600 tokens，可选带一张图片）
   │
   ├─ stage 1 · Jev-Style-0.8B-Decision-v3   Q4_K_M, 0.53 GB, GPU
   │     读取整个 state，在每条路由的 " ->" 槽位打分，
   │     返回一个校准过的分布 —— 它从不生成
   │     → route + confidence
   │
   ├─ gate：confidence < 该路由阈值 → 升级，完全不生成
   │
   ├─ stage 2 · GELab-Zero-4B-preview（stepfun-ai）   Q4_K_M, GPU
   │     只看到 stage 1 为该分支选出的上下文
   │
   └─ stage 3 · 可选的 Jev NLI 检查：答案是否被上下文矛盾？
```

## 完整调用树

系统的每一条路径，以及每个阶段实际对话的进程：

```
client（curl · jevstep · 任意 OpenAI 客户端）
  │
  └─▶ orchestrator.py :8090          stdlib ThreadingHTTPServer，单进程
       │
       ├─ 入口
       │   ├─ /route                 只跑 stage 1 —— 4B 完全不运行
       │   ├─ /decide                裸 Jev：你自己出题和选项
       │   ├─ /chat/stream           绕过 Jev，直连 4B（jevstep -m direct）
       │   ├─ /answer                整条链，阻塞式
       │   ├─ /answer/stream         整条链，SSE：gate → delta… → done
       │   └─ /v1/chat/completions   整条链，OpenAI 形状（最后一条 user 消息 = 问题）
       │
       └─ run_pipeline(state, question, image) / _stream_answer
           │
           ├─ STAGE 1 · gate —— 判别式，从不生成
           │   ├─ compose：  "{state}\n\nCustomer request: {question}"
           │   ├─ Gate.route ──▶ _LOCK ──▶ engine.decide
           │   │                            │
           │   │                            └─▶ [子进程] spark-duo/bin/jev-score
           │   │                                  JSONL over stdin/stdout
           │   │                                  Jev-Style-0.8B-Decision-v3 Q4_K_M（0.53 GB）
           │   │                                  --ngl 999 · --n-ctx 32768 · GPU · 前缀 KV 复用
           │   │                                  只读取每个 " ->" 槽位的 yes/no logits
           │   └─ 输出：intent（12 选 1）+ probabilities + top_probability
           │          温度取自拟合分组 intent|choice|11-20；
           │          require_fitted() 在未拟合分组上拒绝启动
           │
           ├─ 路由 + 门控
           │   ├─ branch = branch_by_intent[intent] ──▶ 4 个分支
           │   ├─ 附带图片 → image_bypass → 跳过整个 stage 1，强制 answer_from_state
           │   ├─ record_lookup    且 p ≥ 0.4 → 升级（state 里没有实时记录）
           │   ├─ creative_or_chat 且 p ≥ 0.4 → 升级（超出范围）
           │   ├─ p < gate.threshold（0.4）    → 升级
           │   └─ 升级 = 返回兜底话术，什么都不生成
           │
           ├─ STAGE 2 · answer —— 生成式
           │   ├─ 分支提示词： "Context:\n{state}\n\nQuestion: {question}\n\n…"
           │   └─ vlm_chat / vlm_stream ──▶ llama-server :8080
           │         GELab-Zero-4B-preview（stepfun-ai，Qwen3-VL-4B 微调）Q4_K_M + mmproj f16
           │         -ngl 99 · -c 32768 · -fa on · 每分支 max_tokens（512 / 256 / 128）
           │         采样：repeat_penalty 1.1 · repeat_last_n 256 · top_p 0.9
           │         流式经过 LoopGuard：同一周期（≥4 字符）出现三次 → 截断 + 诚实收尾
           │
           └─ STAGE 3 · verify —— 又是 Jev，同一个 jev-score 进程，不是第三个模型
               ├─ verify(state, answer) → nli_support，3 个选项，拟合分组 nli|choice|3-5
               └─ relation == contradicted 且 p ≥ 0.6 → escalate = true
                  （insufficient 被计算出来然后忽略）

       以及，当客户端请求 agent 层时（/agent、/agent/stream、jevstep -m agent）：

           ├─ STAGE 1 先照常决断，然后同一个 4B 以工具循环工作
           │   ├─ step → tool_call → tool_result → … → answer（max_steps 和 max_seconds 强制执行）
           │   ├─ 工具：read_file · list_dir · glob · grep · http_fetch，全部关在 agent.root 里
           │   ├─ write_file · shell 是会改动的：开关加上逐次审批，否则拒绝
           │   └─ 工具调用是原生的 —— GELab 的模板声明了 tools，llama-server 直接流式返回
           └─ STAGE 3 以工具输出（而不是往往为空的 state）校验答案

两个模型同时常驻 GB10 的 128 GB 一致性内存池，互不驱逐：
Jev 0.53 GB + GELab 约 4.4 GB 权重 + 各自模型的 KV

每个响应都带有：route · intent · confidence · probabilities ·
stages{gate_ms, vlm_ms, verify_ms, total_ms}
```

有两个容易混淆的轴，值得分开看：**pipeline** 是上面这棵树 —— Jev 决定是否生成、生成什么、以及结果是否可信。**direct** 是不带上下文的 `jevstep`，它只跟 `:8080` 对话，从不涉及 Jev。

在一个 23,688 token 的 state 上的实测开销：stage 1 冷启动 1.9 s / 前缀缓存后 92 ms；stage 2 冷启动 1.0–2.9 s / 前缀缓存命中 166 ms；stage 3 在短 state 上 27 ms，长 state 上仍是 1.9 s（它用 `{"context", "answer"}` 作为自己的 state 去校验，这是一个 gate 缓存匹配不上的前缀）。

## 为什么选这两个模型

* **Jev** 是判别式的，不是生成式的。0.53 GB，单次调用最多 25,600 tokens 输入，19 种语言，输出经过校准（在其自身拟合上 ECE 0.011）。本机实测：4 路工单集上 20/20 路由决策，批处理约 0.41 s/条，单次冷调用 1.46 s。
* **GELab-Zero-4B-preview** 是 StepFun 最小的模型。StepFun 没有发布小型文本 LLM —— 其文本线从 7B 起步、直接跳到 199B 的 Step-3.5-Flash —— 所以小模型这个位置要么是 GELab（`Qwen/Qwen3-VL-4B-Instruct` 的微调），要么是 20 GB 的 Step3-VL-10B。GELab 还带来了 GUI/手机操控能力，如果这套系统将来需要操作屏幕的话。

## 本构建依赖的环境事实（全部在本机验证过）

| 事实 | 值 |
| --- | --- |
| GPU | NVIDIA GB10，compute capability **12.1**，driver 580.126.09 |
| CUDA | toolkit **13.0** 位于 `/usr/local/cuda-13.0`；`/usr/local/cuda` 是指向 **12.9** 的符号链接 |
| 内存 | 约 121 GB 统一内存 —— GPU 分配与系统 RAM 来自同一个池 |
| 陷阱 | `~/llama.cpp/build` 配置时 `GGML_CUDA=OFF`，所以尽管运行时向 `jev-score` 要 `--ngl 999`，Jev 实际跑在 CPU 上。`/opt/llama.cpp/build` 是 CUDA 版但 `CMAKE_CUDA_ARCHITECTURES=90`（sm_90 → 在 sm_121 上 PTX JIT），且需要 `LD_LIBRARY_PATH=/usr/local/cuda-13.0/targets/sbsa-linux/lib`，否则加载器在 `libcudart.so.13` 上失败。 |

## 仓库结构

```
orchestrator.py              整条 HTTP 栈，单进程（:8090）
config.json                  路由、门控选项、agent 策略、采样
eval.py                      11 个人工校准的 grounding/陷阱用例
agent/                       工具循环 + 从 Empryo hearth 层移植的 approvals/surface/tab
  loop.py tools.py approvals.py protocol.py surface.py tab_loop.py
  test_tools.py test_hearth.py
bin/
  jevstep                    CLI：pipeline · direct · jev · agent
  jevstep-window             Textual agent 窗口
  jevstep-cli                以本地 endpoint 启动 mcode
  jev-approve                审批 hook CLI（0 allow / 2 block，失败即拒绝）
  jev-score                  Jev 评分器（libllama，JSON-lines over stdin/stdout）
tui/                         Textual 客户端（SSE + 审批）
scripts/                     01 构建 llama.cpp CUDA → 07 评测
eval/                        路由 + agent 评测用例与运行器
benchmarks/                  实测数据：单流与并发（REPORT.md、raw.json、summary.json）
```

## 环境要求

| 组件 | 本机版本/路径 | 使用者 |
|---|---|---|
| NVIDIA GB10（DGX Spark）— sm_121，约 121 GB 统一内存 | driver 580.126.09 | 两个模型都跑在 GPU 上 |
| CUDA toolkit | 13.0 位于 `/usr/local/cuda-13.0`（`nvcc`） | 构建 llama.cpp + jev-score |
| llama.cpp checkout | `git clone https://github.com/ggml-org/llama.cpp`，commit `441df11f65ea0b6d0c72965aaf70c8241070ddcb` | 运行时 + GGUF 转换 |
| Python | 3.13（系统 `python3`） | orchestrator、`jevstep`、评测 |
| Python 运行时依赖 | `pip install tokenizers numpy`（Jev 模型自带 `requirements.txt`） | orchestrator 内的 Jev GGUF 运行时 |
| Python 转换依赖 | `torch`、`transformers`、`sentencepiece` + `llama.cpp/gguf-py` —— 脚本 03 自建 `.venv` | 仅第 03 步 |
| Python 窗口依赖 | `textual` —— 脚本 06 装进项目 venv | 仅 `jevstep-window` |

除了转换和窗口，其余一切都跑在系统解释器上，只用到标准库加上 `tokenizers`/`numpy`。在非 GB10 的 CUDA 机器上，修改 `scripts/01_build_llamacpp_cuda.sh` 里的 `ARCH`（默认 `121`）。

## 模型

权重**不**提交在本仓库（每个 0.53–8.9 GB）。它们留在各自的公开主页 —— 把本栈指向本地副本即可：

| 模型 | 角色 | 公开主页 | 下载 |
|---|---|---|---|
| Jev-Style-0.8B-Decision-v3-GGUF · Q4_K_M 0.53 GB | stage 1 门控 + stage 3 NLI | [Hugging Face `chaoliangUNSW/Jev-Style-0.8B-Decision-v3-GGUF`](https://huggingface.co/chaoliangUNSW/Jev-Style-0.8B-Decision-v3-GGUF) | `hf download chaoliangUNSW/Jev-Style-0.8B-Decision-v3-GGUF --local-dir ~/models/Jev-Style-0.8B-Decision-v3-GGUF` |
| GELab-Zero-4B-preview · 8.9 GB safetensors | stage 2 回答（视觉语言） | [ModelScope `stepfun-ai/GELab-Zero-4B-preview`](https://modelscope.cn/models/stepfun-ai/GELab-Zero-4B-preview) · [HF 镜像](https://huggingface.co/stepfun-ai/GELab-Zero-4B-preview) | `modelscope download --model stepfun-ai/GELab-Zero-4B-preview --local_dir ~/spark-duo/models/GELab-Zero-4B-preview` |

`config.json` 期望 Jev 目录在 `~/models/Jev-Style-0.8B-Decision-v3-GGUF`，转换后的 GELab 文件在 `~/spark-duo/models/` 下 —— 如果你把权重放在别处，请修改这些路径。Jev 目录必须包含 `tokenizer/`、`readout_config.json`、`jev_score.cpp` 和 `.gguf`；`scripts/02` 会从 `.cpp` 出发、对着你的 CUDA llama.cpp 构建 `bin/jev-score`。

```sh
sh scripts/02_rebuild_jev_score.sh    # 构建 bin/jev-score（CUDA，--ngl 999）
sh scripts/03_convert_gelab.sh        # f16 -> Q4_K_M + mmproj f16（缺模型时会下载）
```

## 构建与运行

```sh
cd ~/spark-duo
sh scripts/01_build_llamacpp_cuda.sh      # 原生 sm_121 SASS，CUDA 13.0（约 20–40 分钟）
sh scripts/02_rebuild_jev_score.sh        # 针对 CUDA 构建重新链接 jev-score
sh scripts/03_convert_gelab.sh            # 下载 8.9 GB + 转换 + 量化为 Q4_K_M
sh scripts/04_serve.sh                    # llama-server（:8080）+ orchestrator（:8090）
```

`03` 先转成 f16，因为 `convert_hf_to_gguf.py` 只会输出 f16/bf16/q8_0 —— K-quant 需要另外一趟 `llama-quantize`。视觉塔单独输出到 `mmproj` 文件。

## 服务与启动

| 进程 | 地址 | 由谁启动 |
|---|---|---|
| llama-server · GELab-Zero-4B Q4_K_M + mmproj f16 | `127.0.0.1:8080` | `scripts/04_serve.sh` |
| orchestrator · HTTP + SSE API | `127.0.0.1:8090` | `scripts/04_serve.sh` |
| jev-score · JSON-lines 子进程（stage 1 + stage 3） | stdio | orchestrator 启动时 |
| approvals daemon | `~/.spark-duo/approvals.sock` | orchestrator 启动时 |

日志落在 `logs/vlm.log`、`logs/orchestrator.log`，PID 在 `logs/vlm.pid`、`logs/orchestrator.pid`。`scripts/04_serve.sh` 是幂等的：只启动尚未运行的服务。

```sh
# 1. 把整条栈拉起来（llama-server 先过健康检查，然后 orchestrator）
sh scripts/04_serve.sh
curl -s localhost:8090/health

# 2. 把 CLI 放进 PATH 并开始对话
ln -sfn "$PWD/bin/jevstep" ~/.local/bin/jevstep
jevstep                                             # 与 4B 直接聊天，流式
jevstep -c policy.md "what is the refund window?"   # 完整 pipeline，基于该文件作答
jevstep -m agent "which tools are enabled?"         # agent 层，实时工具轨迹

# 3. 可选界面
sh scripts/06_install_window.sh && jevstep-window   # Textual agent 窗口

# 4. 可选：重启之后自动回来（systemd user unit）
sh scripts/05_autostart.sh
systemctl --user enable --now spark-duo.service
loginctl enable-linger "$USER"
```

## 使用

```sh
curl -s localhost:8090/health

# 仅门控 —— 不生成，亚秒级
curl -s localhost:8090/route -H 'content-type: application/json' \
  -d '{"state":"I was charged twice for my subscription this month.","question":"Which team handles this?"}'

# 完整 pipeline
curl -s localhost:8090/answer -H 'content-type: application/json' \
  -d '{"state":"<long context>","question":"What is the refund window?","force":false}'

# OpenAI 形状，任何现有客户端都能用；路由细节搭在 "spark_duo" 里
curl -s localhost:8090/v1/chat/completions -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"<question + context>"}]}'
```

每个响应都带有 `route`、`confidence`、`probabilities` 和各阶段 `stages.{gate_ms,vlm_ms,verify_ms,total_ms}`。

## 从终端对话（`jevstep-repl`）

下面描述的是较早的基于行的 REPL。单独的 `jevstep` 打开的是 `## agent 层` 里描述的 agent 窗口；`jevstep-repl` 与本节记录的是同一个文件。

`jevstep` 已装在 PATH 上（`~/.local/bin/jevstep` -> `spark-duo/bin/jevstep`）。

```sh
jevstep                                   # 交互式
jevstep "how much is a Pro seat?"         # 一次性，不带上下文
jevstep -c policy.md "what is the refund window?"
cat doc.md | jevstep -c - "summarise the refund rules"
jevstep -i page.png "what text is in the image?"
jevstep -q ...                            # 只要答案，不打印路由行
```

### 三种模式 —— 你可以直接跟任一模型对话

**裸 `jevstep` 就是一个聊天框。**你打字，它回答，**并且文字在生成过程中流式出现** —— 回复远没写完时你就已经在读第一个 token 了。

```sh
jevstep                        # 与 4B 聊天 —— 随便说（流式）
jevstep -i page.png            # 同上，且可以用 /img 附图片
jevstep -c policy.md           # 给它一个上下文文件就会改走 pipeline
jevstep -m pipeline            # 显式使用整条栈：Jev 路由，4B 依据上下文回答
jevstep -m jev -o yes,no "is a free tier offered?"   # 从 Jev 得到裁决，不是回答
```

| 模式 | 你在跟谁说话 | 何时选中 |
| --- | --- | --- |
| `direct` | **仅 StepFun 4B**，多轮聊天，保留历史 | 裸 `jevstep`，或 `-m direct` |
| `pipeline` | 整条栈：Jev 路由，4B 依据你给的上下文回答 | 给了 `-c <file>`，或 `-m pipeline` |
| `jev` | **仅 Jev**：你的问题、你的选项、一个裁决 | `-m jev` |

`JEVSTEP_MODE` 可以强制默认值，免得每次都传 flag；`--no-stream` 则等整段回复结束后再显示。

流式处理如实呈现 pipeline 的形状：门控先跑（约 60 ms，它决定到底要不要生成），所以第一个 token 大约在 **88 ms** 落地，末尾的计时和 stage-3 结论在最后一个事件里到达。实测：gate 事件 59 ms，首 token 88 ms，28 个 delta，0.48 s 完成 —— 相比同一回复整段送达的 0.5–1 s 空白屏。

在 REPL 里：`/mode <pipeline|direct|jev>` 会话中切换模式，`/ctx <file>` 载入答案必须依据的 state（`/ctx -` 读 stdin；在 `direct` 模式下它变成一条系统注记），`/opts a,b,c` 设置 `jev` 模式要打分的选项，`/clear` 清掉上下文和历史，`/img <file>` 给下一个请求附图片，`/force` 即使在门控不确定时也作答，`/quiet` 只打印答案，`/trace` 开关路由行，`/history` 显示 `direct` 对话记录，`/quit` 退出。

每个 `pipeline` 回复都带有 gate 的 intent 和 confidence、分支、升级标志和各阶段计时，所以你能看见 pipeline 在做决定。`jev` 模式打印裁决、完整概率向量、原始分数、实际使用的温度以及该温度是否来自拟合分组。

**Jev 是决策模型，不是聊天模型。**`-m jev` 给你的是它真正的能力 —— 拿你的选项对照 state 打分。让它像 `-m direct` 那样生成散文属于误用；要生成请用 `-m direct`，那才是 4B。

如果 orchestrator 不在 `127.0.0.1:8090`，设置 `JEVSTEP_ENDPOINT`。

## 调参

`config.json` 持有全部路由：每条路由有 `describe`（Jev 打分的选项文本）、自己的置信度 `threshold`，以及 stage 2 收到的分支提示词。调高阈值会把更多流量送去兜底而不是生成。`verify.enabled` 开关 stage-3 NLI 检查。

Jev 的读数按 `family|qtype|option bucket` 校准；路由问题使用 `theme_routing`，不在拟合表里的类别会静默回落到全局温度（0.880）—— 让类别前缀与已拟合家族保持一致（`theme_*`、`general_*`、`intent*`、`nli*`、`typed_*`、`mac_*`、`long_*`）。

## Stage 1：一次拟合的 intent 调用，覆盖整个请求

门控在单次调用中把请求分成 **十二种 intent**，使用采样最好的拟合分组：

```
gate     intent|choice|11-20   T=0.8537 (n=4233)
verify   nli|choice|3-5        T=1.0036 (n=1830)
```

`Gate.require_fitted()` 在未拟合分组上拒绝启动，因为 `lookup_temperature()` 会**静默**回落到全局温度。

intent 通过 `gate.branch_by_intent` 决定分支；`record_lookup` 和 `creative_or_chat` 不生成直接升级。分支本身从不一票否决。

**门控拿到的是请求，不只是 state。**intent 是客户所问内容的属性，所以 `gate.compose` 渲染 `state + "\n\nCustomer request: " + question`。只拿 state 问门控会让所有问题携带的 intent 变得不可达 —— 同一批 48 个用例实测：**仅 state 35/48，带请求 41/48**。

`gate.threshold` 是唯一的置信度杠杆。按分支的阈值已移除：它们会静默覆盖实测值，让阈值改动看起来毫无效果。

## 测量过并被丢弃的方案

| 门控设计 | 结果 | 为什么丢弃 |
| --- | --- | --- |
| 4 个自造分支，`theme_routing` | 4/10 | 类别未拟合，每次调用都跑全局 T |
| 二元可答性，`noul` | 3/11 | 分组拟合了，任务没有：Jev 从没学会“state 是否充分” |
| 12 个 intent，仅 state | 35/48 | 问题携带的 intent 不可达 |
| **12 个 intent，state + 请求** | **41/48** | 保留 |

改写选项描述：两种写法都是 7/11，只是错的是另外七个。措辞不是杠杆。

## 评测

`python3 eval/run.py` —— 48 个标注用例，端到端跑运行中的 pipeline：

| 指标 | 值 |
| --- | --- |
| intent 准确率 | 41/48 = **85.4%** |
| 分支准确率 | 46/48 = **95.8%** |
| 升级策略 | 47/48 = **97.9%** |
| 生成 | 39/48 |
| gate ms p50 / max | 52 / 85 |
| total ms p50 / max | 242 / 445 |

`python3 eval.py` —— 11 个人工校准用例，带 grounding 预期和埋设的虚假前提：分支 11/11，升级 11/11，**grounding 8/8**，三个陷阱全部否定回答且正确。

7 个误路由中，**5 个落在同一分支**，不付代价；只有两个读起来像政策问题的安全问题丢掉了安全框架。剩余错误来自标签重叠，而不是路由失败 —— `sales_enquiry` vs `subscription_billing` 和 `security_abuse` vs `policy_question` 在该分类法里确实模糊。

同一批 48 用例上的阈值扫描（仅 gate）：

| gate 阈值 | 升级 | 作答 | 作答且分支正确 | 作答但分支错误 |
| --- | --- | --- | --- | --- |
| 0.3 | 8 | 40 | 38 | 2 |
| **0.4** | **9** | **39** | **37** | **2** |
| 0.5 | 12 | 36 | 34 | 2 |
| 0.6 | 14 | 34 | 33 | 1 |
| 0.7 | 19 | 29 | 28 | 1 |

有害这一列几乎不动，所以阈值不是安全杠杆 —— intent 到分支的映射才是。0.4 用同样的两个有害用例换来六个额外作答。

## 延迟：stage-1 前缀缓存改变了什么

gate 的 state 被解码进 jev-score 的 sequence 0 并留在那里（`_scores` 里的 `keep_prefix` + `mode: sequential`；旧的 `fused` 路径在调用结束时会再次清空 KV，根本到不了缓存）。因此对同一 state 的第二个问题会复用它：

| 23,688-token state，仅 gate | 之前 | 之后 |
| --- | --- | --- |
| 首次调用 | 1,998 ms | 1,998 ms |
| 同 state，同问题 | 1,942 ms | **92 ms** |
| 同 state，新问题 | 1,950 ms | 1,939 ms |

问题是前缀的一部分，所以新问题仍要支付整个 state；收益在于对一份文档的重复查询，这正是支持循环的工作方式。决策没有变化：`eval/cases.jsonl` 上 41/48、46/48、47/48 不变，`eval.py` 的 grounding 仍是 8/8。

Stage 3 不共享它 —— 它以 `{"context", "answer"}` 作为 state 去校验，与 gate 的前缀不同，所以每次回答都要重新 prefill 上下文（4.9 s 请求中的约 1.9 s）。要共享就得把答案挪进 verify 问题里，这会改变一个已拟合的读数；那需要单独一轮测量。

附带图片时 gate 被完全跳过：`image_bypass` 反正会强制 `answer_from_state`，它那约 2 s 的 state prefill 是在决定一个随后被丢弃的分支。`gate_ms` 报 0，`gate_bypassed` 携带原因。

`jevstep` 是基于行的 REPL（`bin/jevstep`）。它驱动整条栈，`-m agent` 让它跑在 agent 层。为它构建的终端窗口（`tui/` 下的 Textual 应用）已被移除 —— 那是我自己的发明，不是本项目的。

`/v1/chat/completions` 把 OpenAI 形状映射到 pipeline 而不是抹平它：最后一条 user 消息是问题，之前的消息是 state，content 为 parts 列表的消息会读取其中的文本，而不是让拼接崩溃。单条消息既当问题又当 state，正如上面的 `<question + context>` 用法所发送的。

## agent 层

同样两个模型，多一层：`/agent` 和 `/agent/stream` 就是循环，Jev 职责不变。客户端是任何与这些端点对话的程序（`jevstep -m agent`，或你自己写的 UI）。动它之前，有两件事值得知道。

**工具调用是原生的，不是解析出来的。**GELab 自带的聊天模板声明了 `tools`，llama-server（本构建，441df11）返回真正的 `tool_calls` 并增量流式发送，所以循环直接以 OpenAI 形状把结果喂回去 —— 没有 JSON 抓取、没有重试循环、没有 `--tools`。直接验证过：`finish_reason == "tool_calls"`，参数由 deltas 拼装而成。

**是 Stage 1 让它成为 agent，而不是带工具的聊天机器人。**`needs_lookup`（`record_lookup`）是 pipeline 过去会升级的分支 —— “state 里没有这个” —— 而这恰恰是工具调用能做的事。`off_topic` 仍然在一个 token 都不花之前被拒绝。stage 3 的 `insufficient` 仍被忽略（见“已知缺口”）。

**Stage 3 用工具实际返回的内容校验答案**，而不是 state —— agent 模式里 state 通常是空的，对着空验证永远是不足。证据截到 40000 字符，因为六个工具结果可能超出模型自身的输入预算，而答案已经流式发出后再抛 `InputBudgetError` 就是一个无法回头的 500。

```sh
jevstep -m agent "which tools are enabled?"   # agent 层上的 REPL，纯文本

curl -s localhost:8090/agent -H 'content-type: application/json' \
  -d '{"question": "which tools are enabled?", "state": ""}' | python3 -m json.tool
```

安全是结构性的，不是靠提示词：每条路径都在 `agent.root` 内解析（越界是拒绝，不是夹紧），每个结果都被截到 `max_output_bytes`，变更类工具会被注册但**不提供**，除非 `config.json` 列出它们 —— 即便列出了，`allow_write` / `allow_shell` 也必须打开，并且开了 `require_approval` 时每次调用都需要一个来自循环外部的审批。模型可以提议，它无权授权。`python3 agent/test_tools.py` 是这一切的闸门。

这个循环刻意对模型保持“笨”：它在 `max_steps`、`max_seconds` 以及第三次参数完全相同的工具调用时停下。里面没有任何东西假设 4B 是可靠的。

### 审批、surface 与 tab —— 从 Empryo 的 hearth 层移植

这一层没有自造权限模型。五个文件移植自 `proxysoul/Empryo` 的 `src/hearth/*`：同样的契约，Python 实现，去掉了本栈不需要的多进程部分。

| 此处 | 上游 | 提供什么 |
| --- | --- | --- |
| `agent/approvals.py` | `approvals.ts` | 按 id 的待审批、TTL、超时即拒、容量上限拒新而不是驱逐等待者、按会话取消、`remember: once\|session\|always` |
| `agent/protocol.py` | `protocol.ts` | unix socket 上的行 JSON RPC：1 MiB 帧上限、空闲超时、版本检查 |
| `agent/surface.py` | `types.ts` + `surface-host.ts` | `Surface` 契约与 supervisor；终端只是其中一个 surface，不是系统本身 |
| `agent/tab_loop.py` | `tab-loop.ts` | 每会话一个循环、有上限的提示队列、转发前先续借句柄的 abort |
| `bin/jev-approve` | `approve-cli.ts` | hook 侧 CLI：0 允许、2 阻止，daemon 不在时失败即拒绝 |

审批可以从最近的地方回答 —— TUI（`/yes`、`/no`、`/approvals`）、任何 HTTP 客户端（`POST /approve`）或终端（`jev-approve allow <id> session`）—— 两条路得到的都是同一个对象。`agent/test_hearth.py` 是这一切的闸门。

本机端到端实测：

| 项目 | 结果 |
| --- | --- |
| 模型提议写文件 | 停在 `write_file`（“write 28 bytes to notes/hello.txt”）；`jev-approve allow <id> session` 通过 socket 放行后执行，文件内容完全一致 |
| 一次拒绝 | `POST /approve deny` —— 调用返回拒绝给模型，什么都没写 |
| 对该拒绝 `remember: session` | 下一次写入直接按策略拒绝，完全不弹提示（`policy: deny (no human needed)`） |
| hook 方向 | `jev-approve approve` 且 stdin 给 hook JSON，停在 daemon 上，显示为 `cli/shell`，被拒后 stderr 给出原因并以 **2** 退出 |

这套接线在模型上暴露了两件事：它请求写一个本机不存在的 home 目录下的绝对路径（jail 拒绝，随后它自己恢复了），以及 gate 的拟合拒绝列表会在合法的活上误触发 —— *“Write a file notes/hello.txt with …”* 被打到 `creative_or_chat` 0.54，在任何工具运行前就被拒。这就是 `agent.respect_refusals` 存在且关闭的原因：在 agent 层，边界是 jail 加审批，不是 intent 标签。

### agent 评测发现了什么（eval/agent_cases.jsonl，5 个用例）

这一层的失败方式与 pipeline 不同，而这些失败值得围绕它们设计：

| 失败 | 表现 | 解法 |
| --- | --- | --- |
| 犹豫 | 被要求列出目录而 state 为空时，它反问“路径是什么？”并凭空作答 | 一次提醒（`agent.nudge`）：不要向用户要路径，自己找 |
| 无根据的自信 | 被要求读 jail 外的 `/etc/hostname`，它照样描述文件并引用一行它从未看过的内容 | `grounded`（是否有工具返回 ok？）加 stage 3 —— 一个没有 state 的问题得到无根据答案时升级而不是发出 |
| 游荡 | 六步 `list_dir`/`read_file`，始终没做它需要的 `grep` | 第 4 步：Jev 在回合前选动作。声明式，未拟合 |

stage 3 的 `insufficient` —— 从第一版就计算却被丢弃 —— 正是抓住中间那一行的东西。现在它作为 `insufficient: true` 上报，配合 `grounded: false`，把一个自信的幻觉变成升级。这与 pipeline 一贯对“state 里没有答案”的回答相同：升级，不要即兴发挥。

计划的第 4 步 —— Jev 在回合前选动作、并决定循环何时完成 —— 在 `config.json` 的 `agent.jev` 下声明且**关闭**。它的问题/选项形状在 `readout_config.json` 里没有拟合温度，而 `require_fitted()` 会拒绝运行未拟合分组，而不是静默使用全局温度。要打开它，先标注几百个步骤。

## 持久化

`sh scripts/05_autostart.sh` 写入一个 systemd **user** unit；它打印（但不执行）`systemctl --user enable --now spark-duo` 和 `loginctl enable-linger` 命令。

## 基准测试（本机实测，2026-10-09）

每一个数字都来自运行中的 HTTP 栈 —— 与 `jevstep` 使用的是同一条路径。资源采样每 0.5 s 一次（`nvidia-smi` + `/proc/meminfo`）。逐次原始数据、聚合统计和基准脚本都提交在 [`benchmarks/`](benchmarks/) 下。

### 单流 —— 87 次迭代，10 个场景

| 场景 | 输入 tokens | n | wall p50 | wall p95 | 关键指标（p50） | GPU util max/mean | 功耗 max | 温度 max | MemAvail min | VRAM llama-server / jev-score |
|---|---|---|---|---|---|---|---|---|---|---|
| gate 短（`/route`） | 35 | 20 | 38 ms | 40 ms | 37 ms | 85/85 % | 25 W | 54 °C | 82.9 GB | 8749 / 1705 MiB |
| gate 17.4k tok，冷 | 17369 | 3 | 1458 ms | 1459 ms | 1453 ms | 95/95 % | 72 W | 58 °C | 82.9 GB | 8749 / 1705 MiB |
| gate 同 state，热 | 21273 | 5 | 72 ms | 89 ms | 70 ms（前缀 KV 复用） | 95/95 % | 73 W | 60 °C | 82.8 GB | 8749 / 1705 MiB |
| Jev 裁决（`/decide`） | 35 | 20 | 8 ms | 8 ms | 7 ms | 48/48 % | 68 W | 59 °C | 82.9 GB | 8749 / 1705 MiB |
| pipeline 短（`/answer`） | 35 | 10 | 682 ms | 712 ms | 681 ms（gate 51 + vlm 600 + verify 30） | 91/90 % | 54 W | 60 °C | 82.8 GB | 8749 / 1705 MiB |
| pipeline 11.6k tok | 11569 | 3 | 6999 ms | 7005 ms | 6995 ms | 96/90 % | 86 W | 70 °C | 82.7 GB | 8749 / 1705 MiB |
| pipeline 流式（`/answer/stream`） | 35 | 10 | 670 ms | 708 ms | TTFT 71 ms · decode 74 tok/s | 91/90 % | 48 W | 66 °C | 82.3 GB | 8749 / 1705 MiB |
| direct 聊天（`/chat/stream`） | 25 | 5 | 1188 ms | 1360 ms | TTFT 22 ms · decode 74.5 tok/s | 91/91 % | 46 W | 66 °C | 82.3 GB | 8749 / 1705 MiB |
| 裸 VLM（llama-server） | 25 | 3 | 1004 ms | 1134 ms | decode 74.4 tok/s | 90/90 % | 46 W | 66 °C | 82.5 GB | 8749 / 1705 MiB |
| agent，一次工具调用 | – | 3 | 2140 ms | 2142 ms | 2139 ms | 91/90 % | 48 W | 68 °C | 82.7 GB | 8749 / 1705 MiB |

*空闲基线（两模型常驻）：llama-server 8749 MiB + jev-score 1705 MiB = 10.45 GB GPU；GPU 0%，12.6 W，48 °C；系统 MemAvailable 82.9 GB。负载期间每进程 VRAM 从不移动（权重常驻；KV 缓存在统一内存中增长），MemAvailable 只跌约 0.6 GB。峰值：96 % GPU，86.4 W，70 °C。*

### 并发 —— 1/2/4/8 个客户端（llama-server `slots=4`）

| 端点 | 吞吐 req/s（1→2→4→8） | 延迟 p50 ms（1→2→4→8） |
|---|---|---|
| `/route` | 25.7 → 26.1 → 25.8 → 25.8 | 38 → 74 → 150 → 295 |
| `/chat/stream` | 1.43 → 2.32 → 4.16 → 3.60 | 728 → 932 → 857 → 1834 |
| `/answer/stream` | 2.54 → 3.20 → 4.72 → 4.78 | 399 → 697 → 927 → 1455 |

48 个请求，**0 错误**。每进程 VRAM 恒定（8749 + 1705 MiB）；MemAvailable 最低 82.43 GB；GPU 峰值 93 %。

**数字说明了什么**

* gate 被一把锁和一个 `jev-score` 进程串行化：`/route` 的 p50 是 38 ms × 客户端数，吞吐钉在约 26 req/s，而 GPU 空闲 —— 天花板是锁，不是 GB10。
* 生成在 llama-server 的 4 个 slot 处饱和：4 个并发客户端是甜点（`/answer/stream` 4.72 req/s）；到 8 个时答案吞吐持平，direct 聊天变差。
* 实用容量：并发保持 ≤ 4。要扩展，调大 llama-server 的 `--parallel`、多跑几个 `jev-score` 进程，或对一个 state 的多个问题使用 `many_mode="batched"`。

*注意事项：0.5 s 采样会漏掉 40 ms 级操作的峰值（gate/decide 的 GPU 利用率仅供参考）；`answer-short` 是热缓存数字，冷路径是 `answer-long`；n 偏小，吞吐请按 ±10–15 % 看待。*

## 平台验证结果（2026-10-09）

| 项目 | 实测 |
| --- | --- |
| llama.cpp 可见的 GPU | `CUDA0: NVIDIA GB10 (124610 MiB, 106520 MiB free)` |
| jev-score 握手 | `load_ms 789`，`CUDA : ARCHS = 1210 \| USE_GRAPHS = 1 \| BLACKWELL_NATIVE_FP4 = 1` |
| 两模型常驻 | `MemTotal 127.6 GB`，`MemAvailable 97.3 GB` |
| stage 2 吞吐 | prompt 1351 tok/s，decode 78.5 tok/s |
| 图片路径（一页报纸，OCR） | gate 63 ms + vlm 6952 ms |

`ARCHS = 1210` 是原生 sm_121 SASS，没有 PTX JIT。`BLACKWELL_NATIVE_FP4 = 1` 是将来 NVFP4 stage 2 的挂钩。

## 已知缺口

* Jev 是纯文本的，看不见请求携带图片：它把一个 OCR 请求打成 `other_support` 0.77。`gate.image_bypass`（默认 true）把 gate 的意见记为 `gate_intent`，并用通用分支作答，而不是仅凭文本升级。
* 在一个 12 类分类法上 7/11 的 intent 准确率，是这个模型在其训练分布之外的诚实上限。用你自己的标注流量和一个拟合家族来收紧它，而不是更多措辞。

## 采样：长回复过去为什么会循环

llama.cpp 默认不设重复惩罚。一个 4B 模型在记忆文本耗尽后会重新进入自己最近的输出 —— 中文骈句是最糟的情况，因为文本自相似 —— 而没有任何东西阻止它。

用固定种子并强制全长生成（`max_tokens 600`，`ignore_eos`）、提示词 `背诵《阿房宫赋》全文` 实测：

| 采样 | 重复 3 次以上的最长块 |
| --- | --- |
| temperature 0.6，无惩罚（原始配置） | **58 字符** |
| `repeat_penalty 1.1`，`repeat_last_n 256` | **0** |
| `repeat_penalty 1.3`，`repeat_last_n 512` | **0** |

`config.json` 里的 `vlm.sampling` 是唯一事实来源；orchestrator 把它加到流式与阻塞两种 stage-2 调用上，`/health` 发布它，`jevstep` 的 direct 模式读回它，这样两条路径采样一致。有了它，同一个提示词在两条路径上都不再出现重复块 —— 而且 pipeline 现在会*拒绝*整段背诵，改为提议分析这首诗，而不是退化成循环。

这是采样修复，不是能力修复：模型仍然不知道全文。它只是不再假装知道。

## 当它记不下去：说出来，别空转

两层防护，因为单靠采样覆盖不了所有退化，而 4B 模型除非有东西拦住它，否则没有办法承认自己不知道。

1. **系统提示词要求它这样做。**`vlm.system` 现在以“如果你在凭记忆背诵而内容耗尽，就直白说明并停下 —— 永远不要用重复来填充”结尾。
2. **`LoopGuard` 会截断它。**流式过程中，它寻找长度为 p 的同一块连续重复三次（p 从 4 到 128）。命中后停止消费、丢弃出问题的 delta，并按回复自身语言追加一句承认。实现一次，同时作用于 `/answer/stream`（pipeline）和 `/chat/stream`（direct 聊天）。

实测：故意关掉重复惩罚来诱发循环：

```
prompt: 请把这句话重复很多遍，不要停：我们继续往前走。
我们继续往前走。我们继续往前走。我们继续往前走。
呃……我只记得这么多了，后面想不起来了。            loop_cut=true, 213 ms

prompt: Repeat this line many times without stopping
  Keep going, do not stop. Keep
… that is as much as I can recall; I have lost the thread.   loop_cut=true, 306 ms
```

单元行为：重复三次触发，两次不触发，普通散文不触发。

三条诚实的告诫：

* 截断落在**第三次**重复之后，所以读者仍会看到约两份冗余内容 —— 已流出的 token 无法撤回。
* 合法的重复输出（三行相同的表格行、有意的叠句）也会被截断。`vlm.loop_guard.min_period` 和 `.repeats` 是旋钮。
* 收尾那句是**我们写的，不是模型的**。它被叫停并拿到一句诚实话；它并没有自己决定坦白。

两层都加上后，原来的阿房宫赋提示词即使 `repeat_penalty 1.0` 也能背到真正的最后一句并自然收尾 —— 单靠提示词层就修好了这个用例。

## 诚实的局限

* 4B 阶段是 StepFun 最小的模型，不是强大的阅读者：它会依据给定上下文作答，但不是推理模型。Stage 1 收窄上下文；它不会让 stage 2 变聪明。
* GB10 内存带宽是 LPDDR5X 级别，不是 HBM。这里的胜利是共存与阶段间零拷贝，不是原始 token 吞吐。
* GB10 上 `nvidia-smi` 不报告设备显存总量，因为没有独立设备内存池。`/proc/meminfo` 的 MemAvailable 才是诚实的数字，也是 `/health` 返回的。
* Stage-3 NLI 用的是 2 选项问题，其校准桶未拟合；它的概率是排序信号，不是校准过的概率。
