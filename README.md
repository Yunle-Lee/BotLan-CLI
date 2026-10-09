# Spark Duo — Jev gate + StepFun VLM on the DGX Spark

**English** | [中文](README.zh.md)

A two-stage fast-answer system that keeps **both** models resident in the GB10's coherent
unified memory: a 0.53 GB decision model decides what to do, a 4B vision-language model answers.

> **BotLan CLI on the DGX Spark.** BotLan's front end (this repository) drives exactly this
> stack: the `jevstep` CLI and the agent window talk to Jev + GELab-Zero-4B, both served locally
> on the GB10. Installation, benchmark data and every measured table live below.

```
request (state up to 25,600 tokens, optionally an image)
   │
   ├─ stage 1 · Jev-Style-0.8B-Decision-v3   Q4_K_M, 0.53 GB, GPU
   │     reads the whole state, scores every route at its " ->" slot,
   │     returns a calibrated distribution — it never generates
   │     → route + confidence
   │
   ├─ gate: confidence < route threshold → escalate, no generation at all
   │
   ├─ stage 2 · GELab-Zero-4B-preview (stepfun-ai)   Q4_K_M, GPU
   │     sees only the context stage 1 selected for that branch
   │
   └─ stage 3 · optional Jev NLI check: is the answer contradicted by the context?
```
<img width="1057" height="892" alt="image" src="https://github.com/user-attachments/assets/298b33d4-f08d-4dc4-a831-bdd4b19c78c7" />


## The whole tree

Every path through the system, and the process each stage actually talks to:

```
client (curl · jevstep · any OpenAI client)
  │
  └─▶ orchestrator.py :8090          stdlib ThreadingHTTPServer, one process
       │
       ├─ entry points
       │   ├─ /route                 stage 1 only — the 4B never runs
       │   ├─ /decide                raw Jev: your own question + options
       │   ├─ /chat/stream           bypasses Jev, talks to the 4B (jevstep -m direct)
       │   ├─ /answer                the whole chain, blocking
       │   ├─ /answer/stream         the whole chain, SSE: gate → delta… → done
       │   └─ /v1/chat/completions   the whole chain, OpenAI shape (last user turn = question)
       │
       └─ run_pipeline(state, question, image) / _stream_answer
           │
           ├─ STAGE 1 · gate — discriminative, never generates
           │   ├─ compose:  "{state}\n\nCustomer request: {question}"
           │   ├─ Gate.route ──▶ _LOCK ──▶ engine.decide
           │   │                            │
           │   │                            └─▶ [child process] spark-duo/bin/jev-score
           │   │                                  JSONL over stdin/stdout
           │   │                                  Jev-Style-0.8B-Decision-v3 Q4_K_M (0.53 GB)
           │   │                                  --ngl 999 · --n-ctx 32768 · GPU · prefix KV reuse
           │   │                                  reads only the yes/no logits at each " ->" slot
           │   └─ out: intent (1 of 12) + probabilities + top_probability
           │          temperature from the fitted bucket intent|choice|11-20;
           │          require_fitted() refuses to start on an unfitted group
           │
           ├─ routing + gate
           │   ├─ branch = branch_by_intent[intent] ──▶ 4 branches
           │   ├─ image attached → image_bypass → skip all of stage 1, force answer_from_state
           │   ├─ record_lookup    and p ≥ 0.4 → escalate (the state holds no live record)
           │   ├─ creative_or_chat and p ≥ 0.4 → escalate (out of scope)
           │   ├─ p < gate.threshold (0.4)     → escalate
           │   └─ escalate = return the fallback line, generate nothing
           │
           ├─ STAGE 2 · answer — generative
           │   ├─ branch prompt: "Context:\n{state}\n\nQuestion: {question}\n\n…"
           │   └─ vlm_chat / vlm_stream ──▶ llama-server :8080
           │         GELab-Zero-4B-preview (stepfun-ai, a Qwen3-VL-4B fine-tune) Q4_K_M + mmproj f16
           │         -ngl 99 · -c 32768 · -fa on · max_tokens per branch (512 / 256 / 128)
           │         sampling: repeat_penalty 1.1 · repeat_last_n 256 · top_p 0.9
           │         streaming passes LoopGuard: same period (≥4 chars) three times → cut + honest stop
           │
           └─ STAGE 3 · verify — Jev again, the same jev-score process, not a third model
               ├─ verify(state, answer) → nli_support, 3 options, fitted bucket nli|choice|3-5
               └─ relation == contradicted and p ≥ 0.6 → escalate = true
                  (insufficient is computed and then ignored)

       and, when the client asks for the agent tier (/agent, /agent/stream, jevstep -m agent):

           ├─ STAGE 1 decides as above, then the SAME 4B runs a step loop with tools
           │   ├─ step → tool_call → tool_result → … → answer   (max_steps and max_seconds enforced)
           │   ├─ tools: read_file · list_dir · glob · grep · http_fetch, jailed to agent.root
           │   ├─ write_file · shell are mutating: the switch plus a per-call approval, or refused
           │   └─ tool calls are native — GELab's template declares tools and llama-server streams them
           └─ STAGE 3 verifies the answer against the TOOL OUTPUT, not the (often empty) state

resident together in the GB10's 128 GB coherent pool, neither evicts the other:
Jev 0.53 GB + GELab ≈4.4 GB of weights + each model's own KV

every response carries: route · intent · confidence · probabilities ·
stages{gate_ms, vlm_ms, verify_ms, total_ms}
```

Two axes worth keeping apart: **pipeline** is the tree above — Jev decides whether to generate, what
to generate, and whether the result can be trusted. **direct** is `jevstep` with no context, which
talks to `:8080` alone and never involves Jev.

Measured cost on a 23,688-token state: stage 1 1.9 s cold / 92 ms once its prefix is cached; stage 2
1.0–2.9 s cold / 166 ms on a prefix-cache hit; stage 3 27 ms on short states and still 1.9 s on long
ones (it verifies with `{"context", "answer"}` as its state, a prefix the gate's cache cannot match).

## Why these two

* **Jev** is discriminative, not generative. 0.53 GB, up to 25,600 tokens of input per call,
  19 languages, calibrated output (ECE 0.011 on its own fit). Measured here: 20/20 routing
  decisions on a 4-way ticket set, ~0.41 s/item batched, 1.46 s cold single call.
* **GELab-Zero-4B-preview** is StepFun's smallest model. StepFun publishes no small text LLM —
  their text line starts at 7B and jumps to the 199B Step-3.5-Flash — so the small slot is
  GELab (a `Qwen/Qwen3-VL-4B-Instruct` fine-tune) or the 20 GB Step3-VL-10B. GELab also brings
  GUI/phone-control ability if this system later has to operate a screen.

## Environment facts this build depends on (all verified on this machine)

| Fact | Value |
| --- | --- |
| GPU | NVIDIA GB10, compute capability **12.1**, driver 580.126.09 |
| CUDA | toolkit **13.0** at `/usr/local/cuda-13.0`; `/usr/local/cuda` symlinks to **12.9** |
| Memory | ~121 GB unified — GPU allocations come from the same pool as system RAM |
| The trap | `~/llama.cpp/build` was configured with `GGML_CUDA=OFF`, so Jev ran on the CPU even though the runtime asks `jev-score` for `--ngl 999`. `/opt/llama.cpp/build` is CUDA but `CMAKE_CUDA_ARCHITECTURES=90` (sm_90 → PTX JIT on sm_121) and needs `LD_LIBRARY_PATH=/usr/local/cuda-13.0/targets/sbsa-linux/lib` or the loader fails on `libcudart.so.13`. |

## Repository layout

```
orchestrator.py              the whole HTTP stack in one process (:8090)
config.json                  routes, gate options, agent policy, sampling
eval.py                      11 curated grounding/trap cases
agent/                       the tool loop + the approvals/surface/tab port from Empryo's hearth layer
  loop.py tools.py approvals.py protocol.py surface.py tab_loop.py
  test_tools.py test_hearth.py
bin/
  jevstep                    the CLI: pipeline · direct · jev · agent
  jevstep-window             the Textual agent window
  jevstep-cli                launcher onto mcode with the local endpoint
  jev-approve                approval hook CLI (0 allow / 2 block, fail-closed)
  jev-score                  Jev scorer (libllama, JSON-lines over stdin/stdout)
tui/                         the Textual client (SSE + approvals)
scripts/                     01 build llama.cpp CUDA → 07 eval
eval/                        routing + agent eval cases and runners
benchmarks/                  measured data: single-stream and concurrency (REPORT.md, raw.json, summary.json)
```

## Requirements

| component | version / path on this box | used by |
|---|---|---|
| NVIDIA GB10 (DGX Spark) — sm_121, ~121 GB unified memory | driver 580.126.09 | both models on the GPU |
| CUDA toolkit | 13.0 at `/usr/local/cuda-13.0` (`nvcc`) | building llama.cpp + jev-score |
| llama.cpp checkout | `git clone https://github.com/ggml-org/llama.cpp` at `441df11f65ea0b6d0c72965aaf70c8241070ddcb` | runtime + GGUF conversion |
| Python | 3.13 (system `python3`) | orchestrator, `jevstep`, evals |
| Python runtime deps | `pip install tokenizers numpy` (the Jev model ships a `requirements.txt`) | the Jev GGUF runtime inside the orchestrator |
| Python conversion deps | `torch`, `transformers`, `sentencepiece` + `llama.cpp/gguf-py` — script 03 builds its own `.venv` | step `03` only |
| Python window dep | `textual` — script 06 installs it into the project venv | `jevstep-window` only |

Everything except the conversion and the window runs on the system interpreter with the standard
library plus `tokenizers`/`numpy`. On a non-GB10 CUDA machine, change `ARCH` in
`scripts/01_build_llamacpp_cuda.sh` (default `121`).

## Models

The weights are **not** committed here (0.53–8.9 GB each). They stay in their public homes — point
the stack at local copies:

| model | role | public home | download |
|---|---|---|---|
| Jev-Style-0.8B-Decision-v3-GGUF · Q4_K_M 0.53 GB | stage 1 gate + stage 3 NLI | [Hugging Face `chaoliangUNSW/Jev-Style-0.8B-Decision-v3-GGUF`](https://huggingface.co/chaoliangUNSW/Jev-Style-0.8B-Decision-v3-GGUF) | `hf download chaoliangUNSW/Jev-Style-0.8B-Decision-v3-GGUF --local-dir ~/models/Jev-Style-0.8B-Decision-v3-GGUF` |
| GELab-Zero-4B-preview · 8.9 GB safetensors | stage 2 answers (vision-language) | [ModelScope `stepfun-ai/GELab-Zero-4B-preview`](https://modelscope.cn/models/stepfun-ai/GELab-Zero-4B-preview) · [HF mirror](https://huggingface.co/stepfun-ai/GELab-Zero-4B-preview) | `modelscope download --model stepfun-ai/GELab-Zero-4B-preview --local_dir ~/spark-duo/models/GELab-Zero-4B-preview` |

`config.json` expects the Jev directory at `~/models/Jev-Style-0.8B-Decision-v3-GGUF` and the
converted GELab files under `~/spark-duo/models/` — edit those paths if you keep the weights
elsewhere. The Jev directory must contain `tokenizer/`, `readout_config.json`, `jev_score.cpp` and
the `.gguf`; `scripts/02` builds `bin/jev-score` from the `.cpp` against your CUDA llama.cpp.

```sh
sh scripts/02_rebuild_jev_score.sh    # build bin/jev-score (CUDA, --ngl 999)
sh scripts/03_convert_gelab.sh        # f16 -> Q4_K_M + mmproj f16 (downloads the model if missing)
```

## Build and run

```sh
cd ~/spark-duo
sh scripts/01_build_llamacpp_cuda.sh      # native sm_121 SASS, CUDA 13.0 (~20-40 min)
sh scripts/02_rebuild_jev_score.sh        # relink jev-score against the CUDA build
sh scripts/03_convert_gelab.sh            # download 8.9 GB + convert + quantize to Q4_K_M
sh scripts/04_serve.sh                    # llama-server (:8080) + orchestrator (:8090)
```

`03` converts to f16 first because `convert_hf_to_gguf.py` only emits f16/bf16/q8_0 — K-quants
need the separate `llama-quantize` pass. The vision tower goes to its own `mmproj` file.

## Services and startup

| process | address | started by |
|---|---|---|
| llama-server · GELab-Zero-4B Q4_K_M + mmproj f16 | `127.0.0.1:8080` | `scripts/04_serve.sh` |
| orchestrator · HTTP + SSE API | `127.0.0.1:8090` | `scripts/04_serve.sh` |
| jev-score · JSON-lines child (stage 1 + stage 3) | stdio | the orchestrator at startup |
| approvals daemon | `~/.spark-duo/approvals.sock` | the orchestrator at startup |

Logs land in `logs/vlm.log`, `logs/orchestrator.log`, PIDs in `logs/vlm.pid`,
`logs/orchestrator.pid`. `scripts/04_serve.sh` is idempotent: it only starts what is not running.

```sh
# 1. bring the whole stack up (llama-server, health-gated, then the orchestrator)
sh scripts/04_serve.sh
curl -s localhost:8090/health

# 2. put the CLI on PATH and talk to it
ln -sfn "$PWD/bin/jevstep" ~/.local/bin/jevstep
jevstep                                             # direct chat with the 4B, streamed
jevstep -c policy.md "what is the refund window?"   # full pipeline, grounded in the file
jevstep -m agent "which tools are enabled?"         # agent tier, live tool trace

# 3. optional surfaces
sh scripts/06_install_window.sh && jevstep-window   # Textual agent window

# 4. optional: come back after a reboot (systemd user unit)
sh scripts/05_autostart.sh
systemctl --user enable --now spark-duo.service
loginctl enable-linger "$USER"
```

## Use it

```sh
curl -s localhost:8090/health

# gate only — no generation, sub-second
curl -s localhost:8090/route -H 'content-type: application/json' \
  -d '{"state":"I was charged twice for my subscription this month.","question":"Which team handles this?"}'

# full pipeline
curl -s localhost:8090/answer -H 'content-type: application/json' \
  -d '{"state":"<long context>","question":"What is the refund window?","force":false}'

# OpenAI-shaped, so any existing client works; the routing detail rides in "spark_duo"
curl -s localhost:8090/v1/chat/completions -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"<question + context>"}]}'
```

Every response carries `route`, `confidence`, `probabilities` and per-stage `stages.{gate_ms,vlm_ms,verify_ms,total_ms}`.

## Talk to it from a terminal (`jevstep-repl`)

The command below is the older line-based REPL. `jevstep` on its own opens the agent window described
in `## The agent tier`; `jevstep-repl` is the same file this section documents.

`jevstep` is installed on PATH (`~/.local/bin/jevstep` -> `spark-duo/bin/jevstep`).

```sh
jevstep                                   # interactive
jevstep "how much is a Pro seat?"         # one-shot, no context
jevstep -c policy.md "what is the refund window?"
cat doc.md | jevstep -c - "summarise the refund rules"
jevstep -i page.png "what text is in the image?"
jevstep -q ...                            # answer only, no routing line
```

### Three modes — you can talk to either model directly

**Bare `jevstep` is a chat box.** You type, it answers, **and the text streams in as it is
produced** — you are reading the first token long before the reply is finished.

```sh
jevstep                        # chat with the 4B — say anything (streamed)
jevstep -i page.png            # same, and you can attach an image with /img
jevstep -c policy.md           # handing it a context file selects the pipeline instead
jevstep -m pipeline            # the full stack explicitly: Jev routes, the 4B answers from context
jevstep -m jev -o yes,no "is a free tier offered?"   # a verdict from Jev, not an answer
```

| mode | what you are talking to | picked when |
| --- | --- | --- |
| `direct` | **the StepFun 4B alone**, multi-turn chat, history kept | bare `jevstep`, or `-m direct` |
| `pipeline` | the whole stack: Jev routes, the 4B answers from the context you supply | `-c <file>` given, or `-m pipeline` |
| `jev` | **Jev alone**: your question, your options, a verdict | `-m jev` |

`JEVSTEP_MODE` forces a default if you would rather not pass a flag, and `--no-stream` waits for the
whole reply instead of streaming it.

Streaming is honest about the pipeline's shape: the gate runs first (~60 ms, it decides whether to
generate at all), so the first token lands around **88 ms** and the trailing timings and the stage-3
verdict arrive in a final event. Measured: gate event 59 ms, first token 88 ms, 28 deltas, done at
0.48 s — versus 0.5-1 s of blank screen for the same reply delivered whole.

In the REPL: `/mode <pipeline|direct|jev>` switches mid-session, `/ctx <file>` loads the state the
answer must come from (`/ctx -` reads stdin; in `direct` mode it becomes a system note),
`/opts a,b,c` sets the options `jev` mode scores, `/clear` drops context and history,
`/img <file>` attaches an image to the next request, `/force` answers even when the gate is unsure,
`/quiet` prints only the answer, `/trace` toggles the routing line, `/history` shows the `direct`
transcript, `/quit` leaves.

Every `pipeline` reply carries the gate's intent and confidence, the branch, the escalation flag and
the per-stage timings, so you can see the pipeline deciding. `jev` mode prints the verdict, the full
probability vector, the raw scores, the temperature actually used and whether that temperature came
from a fitted group.

**Jev is a decision model, not a chat model.** `-m jev` gives you its real competence — scoring your
options against a state. Asking it to write prose via `-m direct`-style generation would be misuse;
use `-m direct` for that, which is the 4B.

Set `JEVSTEP_ENDPOINT` if the orchestrator is not on `127.0.0.1:8090`.

## Tuning

`config.json` holds the routes: each route has a `describe` (the option text Jev scores), its own
confidence `threshold`, and the branch prompt stage 2 receives. Raising a threshold sends more
traffic to the fallback instead of generating. `verify.enabled` turns on the stage-3 NLI check.

Jev's readout is calibrated per `family|qtype|option bucket`; the route question uses
`theme_routing`, and a category that is not in the fitted table silently falls back to the global
temperature (0.880) — keep the category prefix consistent with the fitted families
(`theme_*`, `general_*`, `intent*`, `nli*`, `typed_*`, `mac_*`, `long_*`).

## Stage 1: one fitted intent call, over the whole request

The gate classifies into **twelve intents in a single call** under the best-sampled fitted group:

```
gate     intent|choice|11-20   T=0.8537 (n=4233)
verify   nli|choice|3-5        T=1.0036 (n=1830)
```

`Gate.require_fitted()` refuses to start on an unfitted group, because `lookup_temperature()`
falls back to the global temperature **silently**.

The intent decides the branch through `gate.branch_by_intent`; `record_lookup` and
`creative_or_chat` escalate without generating. The branch never vetoes on its own.

**The gate is given the request, not just the state.** The intent is a property of what the
customer asked, so `gate.compose` renders `state + "

Customer request: " + question`. Asking
the gate about the state alone made every question-borne intent unreachable — measured on the same
48 cases: **35/48 state-only vs 41/48 with the request**.

`gate.threshold` is the single confidence lever. Per-branch thresholds were removed: they silently
overrode the measured value and made a threshold change look like it did nothing.

## What was measured and thrown away

| gate design | result | why it was dropped |
| --- | --- | --- |
| 4 invented branches, `theme_routing` | 4/10 | category unfitted, every call ran at the global T |
| binary answerability, `noul` | 3/11 | group fitted, task not: Jev never learned "is the state sufficient" |
| 12 intents, state only | 35/48 | question-borne intents unreachable |
| **12 intents, state + request** | **41/48** | kept |

Rewording the option descriptions: 7/11 either way, only a different seven. Wording is not the lever.

## Evaluation

`python3 eval/run.py` — 48 labelled cases, end to end through the running pipeline:

| metric | value |
| --- | --- |
| intent accuracy | 41/48 = **85.4%** |
| branch accuracy | 46/48 = **95.8%** |
| escalation policy | 47/48 = **97.9%** |
| generated | 39/48 |
| gate ms p50 / max | 52 / 85 |
| total ms p50 / max | 242 / 445 |

`python3 eval.py` — the 11 hand-curated cases with grounding expectations and planted false
premises: branch 11/11, escalation 11/11, **grounding 8/8**, all three traps answered negatively and
correctly.

Of the 7 misroutes, **5 land on the same branch** and cost nothing; only two safety questions that
read like policy questions lose the safety framing. The residual error is overlapping labels, not a
routing failure — `sales_enquiry` vs `subscription_billing` and `security_abuse` vs
`policy_question` are genuinely ambiguous in the taxonomy.

Threshold sweep over the same 48 cases (gate alone):

| gate threshold | escalated | answered | answered, right branch | answered, WRONG branch |
| --- | --- | --- | --- | --- |
| 0.3 | 8 | 40 | 38 | 2 |
| **0.4** | **9** | **39** | **37** | **2** |
| 0.5 | 12 | 36 | 34 | 2 |
| 0.6 | 14 | 34 | 33 | 1 |
| 0.7 | 19 | 29 | 28 | 1 |

The harmful column barely moves, so the threshold is not the safety lever — the intent-to-branch
mapping is. 0.4 buys six more answered questions for the same two harmful cases.

## Latency: what the stage-1 prefix cache changed

The gate's state is decoded into jev-score's sequence 0 and stays there (`keep_prefix` + `mode:
sequential` in `_scores`; `fused`, the old path, clears the KV again at the end of the call and never
reaches the cache at all). A second question about the same state therefore reuses it:

| 23,688-token state, gate only | before | after |
| --- | --- | --- |
| first call | 1,998 ms | 1,998 ms |
| same state, same question | 1,942 ms | **92 ms** |
| same state, new question | 1,950 ms | 1,939 ms |

The question is part of the prefix, so a new one still pays for the whole state; the win is repeated
queries over one document, which is what a support loop does. The decisions do not move: 41/48,
46/48 and 47/48 are unchanged over `eval/cases.jsonl`, and `eval.py` still scores grounding 8/8.

Stage 3 does not share it — it verifies with `{"context", "answer"}` as its state, a different prefix
from the gate's, so it re-prefills the context on every answer (~1.9 s of a 4.9 s request). Sharing
means moving the answer into the verify question, which changes a fitted readout; that needs its own
measurement round.

With an image attached the gate is skipped outright: `image_bypass` forces `answer_from_state` anyway,
so its ~2 s state prefill was deciding a branch that was then thrown away. `gate_ms` reports 0 and
`gate_bypassed` carries the reason.

`jevstep` is the line-based REPL (`bin/jevstep`). It drives the whole stack, and `-m agent` puts it on
the agent tier. The terminal window that was built for it (a Textual app under `tui/`) was removed -
it was my own invention, not this project's.

`/v1/chat/completions` maps the OpenAI shape onto the pipeline instead of flattening it: the last
user turn is the question, the turns before it are the state, and a message whose content is a list of
parts is read for its text rather than crashing the join. A single message is both, exactly as the
`<question + context>` usage above sends it.

## The agent tier

The same two models, an extra layer: `/agent` and `/agent/stream` are the loop, and Jev keeps its job.
The client is whatever talks to those endpoints (`jevstep -m agent`, or a UI you write). Two things
are worth knowing before touching it.

**Tool calls are native, not parsed.** GELab's bundled chat template declares `tools` and llama-server
(this build, 441df11) returns real `tool_calls`, streaming them incrementally, so the loop feeds
results straight back in the OpenAI shape — no JSON scraping, no retry loop, no `--tools`. Checked
directly: `finish_reason == "tool_calls"` with the arguments assembled from deltas.

**Stage 1 is what makes it an agent rather than a chatbot with tools.** `needs_lookup`
(`record_lookup`) is the branch the pipeline used to escalate on — "the state does not hold this" —
and it is exactly the work a tool call can do. `off_topic` is still refused before a token is spent.
`insufficient` from stage 3 is still ignored (see Known gaps).

**Stage 3 checks the answer against what the tools actually returned**, not against the state, which
in agent mode is usually empty — verifying against nothing would always report insufficient. The
evidence is capped at 40000 characters, because six tool results can outrun the model's own input
budget and an `InputBudgetError` after the answer has already streamed is a 500 with no way back.

```sh
jevstep -m agent "which tools are enabled?"   # the REPL on the agent tier, plain text

curl -s localhost:8090/agent -H 'content-type: application/json' \
  -d '{"question": "which tools are enabled?", "state": ""}' | python3 -m json.tool
```

Safety is structural, not prompt-based: every path is resolved inside `agent.root` (an escape is
refused, not clamped), every result is truncated to `max_output_bytes`, and the mutating tools are
registered but **not offered** unless `config.json` lists them — and even then `allow_write` /
`allow_shell` must be on and, with `require_approval`, each call needs an approval that came from
outside the loop. The model proposes; it cannot authorise. `python3 agent/test_tools.py` is the gate
for all of that.

The loop is deliberately dumb about the model: it stops on `max_steps`, on `max_seconds`, and on a
third identical tool call with identical arguments. Nothing in it assumes the 4B is reliable.

### Approvals, surfaces and tabs — ported from Empryo's hearth layer

The tier does not roll its own permission model. Five files are a port of `proxysoul/Empryo`
`src/hearth/*`: same contracts, Python, minus the multi-process parts this stack does not need.

| here | upstream | what it gives |
| --- | --- | --- |
| `agent/approvals.py` | `approvals.ts` | pending approvals by id, TTL, deny-on-timeout, a cap that denies instead of evicting a waiter, cancel-for-session, `remember: once\|session\|always` |
| `agent/protocol.py` | `protocol.ts` | line-JSON RPC over a unix socket: 1 MiB frame cap, idle timeout, version check |
| `agent/surface.py` | `types.ts` + `surface-host.ts` | the `Surface` contract and the supervisor; the terminal is one surface, not the system |
| `agent/tab_loop.py` | `tab-loop.ts` | one loop per session, capped prompt queue, abort that renews its handle before forwarding |
| `bin/jev-approve` | `approve-cli.ts` | the hook-side CLI: 0 allow, 2 block, fail-closed when the daemon is gone |

An approval is answered from wherever is nearest — the TUI (`/yes`, `/no`, `/approvals`), any HTTP
client (`POST /approve`), or a terminal (`jev-approve allow <id> session`) — and the answer is
the same object either way. `agent/test_hearth.py` is the gate for all of it.

Measured, end to end on this box:

| what | result |
| --- | --- |
| the model proposes a write | parks on `write_file` ("write 28 bytes to notes/hello.txt"); `jev-approve allow <id> session` over the socket lets it run, file contents exact |
| a denial | `POST /approve deny` — the call returns the refusal to the model, nothing is written |
| `remember: session` on that denial | the next write is refused by policy with no prompt at all (`policy: deny (no human needed)`) |
| the hook direction | `jev-approve approve` with hook JSON on stdin parks on the daemon, shows up as `cli/shell`, and exits **2** with the reason on stderr once denied |

Two things this wiring exposed in the model: it asked to write an absolute path under a home
directory that does not exist on this machine (the jail refused it, then it recovered), and the
gate's fitted refusal list fires on legitimate work — *"Write a file notes/hello.txt with …"* scores
`creative_or_chat` at 0.54 and was refused before any tool ran. That is why `agent.respect_refusals`
exists and is off: in the agent tier the boundary is the jail plus the approval, not the intent label.

### What the agent eval found (eval/agent_cases.jsonl, 5 cases)

The tier fails differently from the pipeline, and those failures are the ones worth designing around:

| failure | what it looks like | what answers it |
| --- | --- | --- |
| hesitation | asked to list a directory with nothing in the state, it replies "what is the path?" and answers from nothing | one nudge (`agent.nudge`): do not ask the user for a path, find it yourself |
| ungrounded confidence | asked to read `/etc/hostname`, which is outside the jail, it describes the file anyway and cites a line it never saw | `grounded` (did any tool return ok?) plus stage 3 — an ungrounded answer to a question with no state escalates instead of shipping |
| wandering | six steps of `list_dir`/`read_file` and never the `grep` it needed | step 4: Jev picks the action before the turn. Declared, not fitted |

`insufficient` from stage 3 — computed since the first version and thrown away — is what catches the
middle row. It is now reported as `insufficient: true` and, with `grounded: false`, turns a confident
hallucination into an escalation. That is the same answer the pipeline always gave for a state that
does not hold the answer: escalate, do not improvise.

Step 4 of the plan — Jev choosing the action before the turn, and deciding when the loop is done —
is declared in `config.json` under `agent.jev` and **off**. Its question/options shape has no fitted
temperature in `readout_config.json`, and `require_fitted()` refuses to run an unfitted group rather
than silently using the global one. Turning those on means labelling a few hundred steps first.

## Persistence

`sh scripts/05_autostart.sh` writes a systemd **user** unit; it prints (but does not run) the
`systemctl --user enable --now spark-duo` and `loginctl enable-linger` commands.

## Benchmarks (measured on this box, 2026-10-09)

Every number comes from the running HTTP stack — the same path `jevstep` uses. Resource sampling
every 0.5 s (`nvidia-smi` + `/proc/meminfo`). Raw per-iteration data, aggregate stats and the
benchmark scripts are committed under [`benchmarks/`](benchmarks/).

### Single stream — 87 iterations, 10 scenarios

| scenario | input tokens | n | wall p50 | wall p95 | key metric (p50) | GPU util max/mean | power max | temp max | MemAvail min | VRAM llama-server / jev-score |
|---|---|---|---|---|---|---|---|---|---|---|
| gate short (`/route`) | 35 | 20 | 38 ms | 40 ms | 37 ms | 85/85 % | 25 W | 54 °C | 82.9 GB | 8749 / 1705 MiB |
| gate 17.4k tok, cold | 17369 | 3 | 1458 ms | 1459 ms | 1453 ms | 95/95 % | 72 W | 58 °C | 82.9 GB | 8749 / 1705 MiB |
| gate same state, warm | 21273 | 5 | 72 ms | 89 ms | 70 ms (prefix KV reuse) | 95/95 % | 73 W | 60 °C | 82.8 GB | 8749 / 1705 MiB |
| Jev decide (`/decide`) | 35 | 20 | 8 ms | 8 ms | 7 ms | 48/48 % | 68 W | 59 °C | 82.9 GB | 8749 / 1705 MiB |
| pipeline short (`/answer`) | 35 | 10 | 682 ms | 712 ms | 681 ms (gate 51 + vlm 600 + verify 30) | 91/90 % | 54 W | 60 °C | 82.8 GB | 8749 / 1705 MiB |
| pipeline 11.6k tok | 11569 | 3 | 6999 ms | 7005 ms | 6995 ms | 96/90 % | 86 W | 70 °C | 82.7 GB | 8749 / 1705 MiB |
| pipeline stream (`/answer/stream`) | 35 | 10 | 670 ms | 708 ms | TTFT 71 ms · decode 74 tok/s | 91/90 % | 48 W | 66 °C | 82.3 GB | 8749 / 1705 MiB |
| direct chat (`/chat/stream`) | 25 | 5 | 1188 ms | 1360 ms | TTFT 22 ms · decode 74.5 tok/s | 91/91 % | 46 W | 66 °C | 82.3 GB | 8749 / 1705 MiB |
| raw VLM (llama-server) | 25 | 3 | 1004 ms | 1134 ms | decode 74.4 tok/s | 90/90 % | 46 W | 66 °C | 82.5 GB | 8749 / 1705 MiB |
| agent, one tool call | – | 3 | 2140 ms | 2142 ms | 2139 ms | 91/90 % | 48 W | 68 °C | 82.7 GB | 8749 / 1705 MiB |

*Idle baseline (both models resident): llama-server 8749 MiB + jev-score 1705 MiB = 10.45 GB GPU;
GPU 0 %, 12.6 W, 48 °C; system MemAvailable 82.9 GB. During load the per-process VRAM never moves
(weights are resident; the KV cache grows inside unified memory) and MemAvailable dips only ~0.6 GB.
Peak: 96 % GPU, 86.4 W, 70 °C.*

### Concurrency — 1/2/4/8 clients (llama-server `slots=4`)

| endpoint | throughput req/s (1→2→4→8) | latency p50 ms (1→2→4→8) |
|---|---|---|
| `/route` | 25.7 → 26.1 → 25.8 → 25.8 | 38 → 74 → 150 → 295 |
| `/chat/stream` | 1.43 → 2.32 → 4.16 → 3.60 | 728 → 932 → 857 → 1834 |
| `/answer/stream` | 2.54 → 3.20 → 4.72 → 4.78 | 399 → 697 → 927 → 1455 |

48 requests, **0 errors**. Per-process VRAM constant (8749 + 1705 MiB); MemAvailable min 82.43 GB;
GPU peak 93 %.

**What the numbers say**

* The gate is serialized behind one lock and one `jev-score` process: `/route` p50 is 38 ms × the
  client count and throughput is pinned at ~26 req/s while the GPU idles — the ceiling is the lock,
  not the GB10.
* Generation saturates at llama-server's 4 slots: 4 concurrent clients is the sweet spot
  (`/answer/stream` 4.72 req/s); at 8 the answer throughput is flat and direct chat gets worse.
* Practical capacity: keep concurrency ≤ 4. To scale, raise `--parallel` on llama-server, run more
  `jev-score` processes, or use `many_mode="batched"` for several questions over one state.

*Caveats: 0.5 s sampling misses the peak of 40 ms operations (gate/decide GPU utilisation is
indicative only); `answer-short` is a warm-cache number, the cold path is `answer-long`; n is small,
so treat throughput as ±10–15 %.*

## Verified platform results (2026-10-09)

| what | measured |
| --- | --- |
| GPU visible to llama.cpp | `CUDA0: NVIDIA GB10 (124610 MiB, 106520 MiB free)` |
| jev-score handshake | `load_ms 789`, `CUDA : ARCHS = 1210 | USE_GRAPHS = 1 | BLACKWELL_NATIVE_FP4 = 1` |
| both models resident | `MemTotal 127.6 GB`, `MemAvailable 97.3 GB` |
| stage 2 throughput | prompt 1351 tok/s, decode 78.5 tok/s |
| image path (page of newsprint, OCR) | gate 63 ms + vlm 6952 ms |

`ARCHS = 1210` is native sm_121 SASS, no PTX JIT. `BLACKWELL_NATIVE_FP4 = 1` is the hook for an
NVFP4 stage 2 later.


## Known gaps

* Jev is text-only, so it cannot see that a request carries an image: it scored an OCR request
  `other_support` at 0.77. `gate.image_bypass` (default true) records the gate's opinion as
  `gate_intent` and answers with the general branch instead of escalating on text alone.
* Intent accuracy of 7/11 on a 12-way taxonomy is the honest ceiling for this model out of its
  training distribution. Tighten it with your own labelled traffic and a fitted family, not with
  more wording.

## Sampling: why long replies used to loop

llama.cpp serves with **no repetition penalty by default**. A 4B model that runs out of memorised
text re-enters its own most recent output — classical Chinese parallel couplets are the worst case,
because the text is self-similar — and nothing stops it.

Measured with a fixed seed and forced full-length generation (`max_tokens 600`, `ignore_eos`), prompt
`背诵《阿房宫赋》全文`:

| sampling | longest block repeated 3+ times |
| --- | --- |
| temperature 0.6, no penalty (the original) | **58 chars** |
| `repeat_penalty 1.1`, `repeat_last_n 256` | **0** |
| `repeat_penalty 1.3`, `repeat_last_n 512` | **0** |

`vlm.sampling` in `config.json` is the single source of truth; the orchestrator adds it to both the
streaming and the blocking stage-2 calls, `/health` publishes it, and `jevstep`'s direct mode reads it
back so both paths sample identically. With it, the same prompt produces no repeated block on either
path — and the pipeline now *declines* the full recitation and offers to analyse the poem instead of
degenerating into a loop.

This is a sampling fix, not a capability fix: the model still does not know the whole text. It just
stops pretending.

## When it runs out: say so, do not spin

Two layers, because sampling alone does not cover every degeneration and a 4B model has no way to
admit ignorance unless something stops it.

1. **The system prompt tells it to.** `vlm.system` now ends with "if you are reciting from memory and
   it runs out, say so plainly and stop - never repeat yourself to fill space."
2. **`LoopGuard` cuts it off.** While streaming, it looks for the same block of p characters repeated
   three times in a row, for p from 4 to 128. On a hit it stops consuming, drops the offending delta,
   and appends an admission — in the reply's own language. Implemented once and applied to both
   `/answer/stream` (pipeline) and `/chat/stream` (direct chat).

Live, with the repetition penalty deliberately switched off to invite a loop:

```
prompt: 请把这句话重复很多遍，不要停：我们继续往前走。
我们继续往前走。我们继续往前走。我们继续往前走。
呃……我只记得这么多了，后面想不起来了。            loop_cut=true, 213 ms

prompt: Repeat this line many times without stopping
 Keep going, do not stop. Keep
… that is as much as I can recall; I have lost the thread.   loop_cut=true, 306 ms
```

Unit behaviour: three repeats fire, two do not, ordinary prose does not.

Three honest caveats:

* The cut lands after the **third** repeat, so the reader still sees about two redundant copies —
  streamed tokens cannot be unsent.
* Legitimately repetitive output (three identical table rows, a deliberate refrain) will also be cut.
  `vlm.loop_guard.min_period` and `.repeats` are the dials.
* The closing sentence is **ours, not the model's**. It was stopped and handed an honest line; it did
  not decide to confess.

With both layers in, the original 阿房宫赋 prompt now recites to the real last line and closes
naturally even with `repeat_penalty 1.0` — the prompt layer alone fixed that case.

## Honest limits

* The 4B stage is StepFun's smallest, not a strong reader: it will answer from the context it is
  given but is not a reasoning model. Stage 1 narrows the context; it does not make stage 2 smarter.
* GB10 memory bandwidth is LPDDR5X-class, not HBM. The win here is co-residency and zero-copy
  between stages, not raw token throughput.
* `nvidia-smi` reports no device memory total on GB10 because there is no separate device pool.
  `/proc/meminfo` MemAvailable is the honest number and is what `/health` returns.
* Stage-3 NLI uses a 2-option question, whose calibration bucket was not fitted; its probability
  is a ranking signal, not a calibrated one.
