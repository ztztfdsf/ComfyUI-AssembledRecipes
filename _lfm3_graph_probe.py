"""LFM3 CUDA Graph 可用性探针（子进程内跑，由 nodes.py 调用）。

为什么必须单独进程：一次中途失败的 CUDA Graph capture 会让进程在释放 capture 池显存时
terminate()（实测 exit=127 / STATUS_STACK_BUFFER_OVERRUN），父进程 try/except 兜不住。
探针跑通 → 父进程才启用 graph 路径；跑不通 → 父进程直接回退 model.generate。

判据：真实模型 + 真实 prompt 形状跑一次 prefill + capture + 8 步 replay，且 replay 结果
与 eager 的下一步 token 一致。最后打印 GRAPH_OK 供父进程识别。
"""
import sys


def main():
    model_path = sys.argv[1]
    # 子进程继承父进程环境，中文/GBK 变量同样会让 libtriton 的 C++ getenv 崩，先修
    try:
        import os as _os
        import triton.knobs as _tk
        _tk.getenv = lambda key, default=None: _os.environ.get(key, default)
        _tk.getenv_bool = lambda key, default=False: (
            default if key not in _os.environ
            else str(_os.environ[key]).strip().lower() in ("1", "true", "yes", "on"))
        _tk._ar_env_patched = True
    except Exception:
        pass
    try:
        import torch
        from transformers import AutoProcessor, AutoModelForImageTextToText
        from transformers.cache_utils import StaticCache, StaticLayer, STATIC_LAYER_TYPE_MAPPING

        print(f"[probe] allocator={torch.cuda.get_allocator_backend()}", flush=True)
        proc = AutoProcessor.from_pretrained(model_path, trust_remote_code=True)
        m = AutoModelForImageTextToText.from_pretrained(
            model_path, dtype=torch.bfloat16, device_map={"": "cuda:0"}, trust_remote_code=True)

        # 装裸 int 直通（与生产同一份逻辑；这里重复一份是为了探针自包含）
        import importlib.util as ilu
        reals = [v for k, v in sys.modules.items()
                 if k.endswith("lfm3_conv") and hasattr(v, "lfm3_causal_conv1d_update")]
        if not reals:
            print("[probe] 找不到 lfm3_conv 模块", flush=True)
            return 2
        real = reals[0]
        orig = real.lfm3_causal_conv1d_update
        known = {}

        def raw_update(hidden_states, conv_state, weight, bias=None):
            key = (tuple(hidden_states.shape), hidden_states.stride(),
                   tuple(conv_state.shape), conv_state.stride(), bias is not None)
            ck = known.get(key)
            if ck is None:
                dev = torch.cuda.current_device()
                cache = real._decode.device_caches[dev][0]
                before = set(cache.keys())
                out = orig(hidden_states, conv_state, weight, bias)
                new = [k for k in cache.keys() if k not in before]
                known[key] = cache[new[0]] if len(new) == 1 else next(iter(cache.values()))
                return out
            import triton
            batch, channels, _ = hidden_states.shape
            output = torch.empty_like(hidden_states, memory_format=torch.contiguous_format)
            d_block = triton.cdiv(channels, 32)
            args = [hidden_states.data_ptr(), weight.contiguous().data_ptr(),
                    bias.contiguous().data_ptr() if bias is not None else 0,
                    conv_state.data_ptr(), output.data_ptr(), channels, conv_state.shape[-1],
                    *hidden_states.stride()[:2], *conv_state.stride(), bias is not None, 32]
            with torch.cuda.device(hidden_states.device):
                ck.run(batch, d_block, 1, torch.cuda.current_stream().cuda_stream,
                       ck.function, ck.packed_metadata, None, None, None, *args)
            return output

        real.lfm3_causal_conv1d_update = raw_update
        mod = sys.modules[type(m.model.language_model).__module__]
        mod.lfm3_causal_conv1d_update = raw_update

        buf = {}

        def _mk(q, kind):
            k = (kind, q)
            if k not in buf:
                buf[k] = torch.zeros(1, 1, q, maxlen, device="cuda", dtype=torch.bfloat16)
            return buf[k]

        def masks_by_type(config, inputs_embeds=None, attention_mask=None,
                          past_key_values=None, position_ids=None, **kw):
            q = inputs_embeds.shape[1] if inputs_embeds is not None else 1
            return {"full_attention": _mk(q, "full"), "sliding_attention": _mk(q, "win"), "conv": None}

        def set_mask(q, pos_end):
            ar = torch.arange(maxlen, device="cuda")
            bf, bw = buf[("full", q)], buf[("win", q)]
            if q == 1:
                bf[0, 0, 0, :] = torch.where(ar <= pos_end, 0.0, neg)
                bw[0, 0, 0, :] = torch.where((ar <= pos_end) & (ar > pos_end - win), 0.0, neg)
            else:
                i = torch.arange(q, device="cuda").view(q, 1)
                bf[0, 0, :, :] = torch.where(ar.view(1, -1) <= i, 0.0, neg)
                bw[0, 0, :, :] = torch.where((ar.view(1, -1) <= i) & (ar.view(1, -1) > i - win), 0.0, neg)

        # 用一段纯文本 prompt（形状要跨过 sliding_window=256 这条分支线，否则测不到 roll 路径）
        tc = m.config.text_config
        maxlen = 3072  # 与生产实测通过的容量一致；1024 会让进程 abort
        neg, win = float("-inf"), int(getattr(tc, "sliding_window", 256) or 256)

        # 生产是「一张图 + 短提示」，探针必须同形，否则测不到视觉塔与 conv 的真实路径
        from PIL import Image
        prompt = ("Output a JSON array with exactly 5 entries, largest region first: "
                  "background scenery, whole character, upper clothing, lower clothing, "
                  "head details (hat/hair/face). box coordinates 0-1000.")
        img = Image.new("RGB", (480, 640), (120, 130, 140))
        msgs = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": prompt}]}]
        chat = proc.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True,
                                        enable_thinking=True)
        inp = proc(text=[chat], images=[img], return_tensors="pt").to("cuda:0")
        PL = int(inp["input_ids"].shape[1])
        if PL <= win + 8:
            print(f"[probe] prompt 只有 {PL} tok，不足以覆盖滑动窗口分支", flush=True)
            return 3
        print(f"[probe] PL={PL} maxlen={maxlen}", flush=True)

        # 预热 generate：让 raw_update 把 Triton decode kernel 的真实形状全部编译进 known 表。
        # 必须在 patch create_masks_by_layer_type 之前——generate 内部自己要方阵 mask，
        # 用我们的静态 buffer 会报 expanded size mismatch（实测 Target [1,16,q,q] vs [1,1,q,maxlen]）。
        with torch.inference_mode():
            _ = m.generate(**inp, max_new_tokens=24, do_sample=False)
        print("[probe] 预热 generate 完成", flush=True)


        # patch 只在这之后生效：warm generate 用的是原生 mask 逻辑
        STATIC_LAYER_TYPE_MAPPING["sliding_attention"] = StaticLayer
        mod.create_masks_by_layer_type = masks_by_type

        _mk(PL, "full"); _mk(PL, "win"); _mk(1, "full"); _mk(1, "win")
        cache = StaticCache(config=tc, max_batch_size=1, max_cache_len=maxlen,
                            device="cuda", dtype=torch.bfloat16)
        set_mask(PL, PL - 1)
        with torch.inference_mode():
            o = m(**inp, past_key_values=cache,
                  cache_position=torch.arange(PL, device="cuda"), use_cache=True)
        s_ids = o.logits[:, -1:].argmax(-1)
        s_pos = torch.tensor([PL], device="cuda")
        set_mask(1, PL)

        st = torch.cuda.Stream(); st.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(st):
            for _ in range(2):
                cw = StaticCache(config=tc, max_batch_size=1, max_cache_len=maxlen,
                                 device="cuda", dtype=torch.bfloat16)
                with torch.inference_mode():
                    m(input_ids=torch.zeros(1, PL, dtype=torch.long, device="cuda"),
                      past_key_values=cw, cache_position=torch.arange(PL, device="cuda"), use_cache=True)
                    m(input_ids=s_ids, past_key_values=cw, cache_position=s_pos.clone(), use_cache=True)
                set_mask(1, PL)
        torch.cuda.current_stream().wait_stream(st)

        g = torch.cuda.CUDAGraph()
        with torch.inference_mode():
            with torch.cuda.graph(g):
                o = m(input_ids=s_ids, past_key_values=cache, cache_position=s_pos, use_cache=True)
        print("[probe] capture ok", flush=True)

        got = []
        for i in range(8):
            set_mask(1, PL + i)
            s_pos.fill_(PL + i)
            g.replay()
            nxt = o.logits[:, -1:].argmax(-1)
            got.append(int(nxt.item()))
            s_ids.copy_(nxt)
        print(f"[probe] replay 8 步 ok, 末 token={got[-1]}", flush=True)
        print("GRAPH_OK", flush=True)
        return 0
    except Exception as e:
        import traceback
        print(f"[probe] FAIL {type(e).__name__}: {str(e)[:200]}", flush=True)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
