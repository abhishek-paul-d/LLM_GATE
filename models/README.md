# Model specs

Each YAML file describes one side of a comparison: its weights, vLLM serving settings, and default request settings. Select a spec by its `name`, which must match the file name without `.yaml`; the prompt and test suite are selected per run, not here.

## Shipped specs

Both shipped specs use FP8 weights with bf16 activations.

| Name | Hugging Face model | License | Gated | Notes |
| --- | --- | --- | --- | --- |
| `llama-3.1-8b-instruct-fp8` | `meta-llama/Llama-3.1-8B-Instruct` | Llama 3.1 Community License | yes | Accept the license on Hugging Face and add a Colab secret named `HF_TOKEN`. |
| `qwen3-8b-fp8` | `Qwen/Qwen3-8B` | Apache-2.0 | no | Thinking is disabled with `chat_template_kwargs.enable_thinking: false`. |

## Add or change a model

1. Copy an existing spec to `models/<new-name>.yaml`.
2. Set `name` to `<new-name>`. Use lowercase letters, digits, `.`, `_`, or `-`.
3. Edit `model.id`, `revision`, `license`, `gated`, and the `serving` and `request` fields.
4. Set `serving.weights_gb` to an estimate: roughly 2 GB per billion parameters in bf16, about 1.15 GB per billion for FP8 (embeddings and output layer stay bf16), or about 0.7 GB per billion for 4-bit AWQ/GPTQ.
5. Validate the spec with `gate models show <new-name>`.
6. List available specs with `gate models list`.

## Field notes

- `revision`: use a 40-character commit SHA for release runs. `main` is fine while developing; the run manifest records the resolved SHA.
- `structured_output`: `none` measures whether the model produces valid JSON by itself. `json_schema` makes vLLM constrain decoding; changing it changes what is being tested.
- `chat_template_kwargs`: Qwen3 specs must set `enable_thinking` explicitly or loading fails.
- `extra_args`: extra `vllm serve` flags. Flags managed by the spec, such as `--port`, `--max-model-len`, `--seed`, and `--gpu-memory-utilization`, are refused.
- `gpu_memory_utilization`: leave it `null` so the runner chooses it from the execution mode.

## Precision, GPU memory and execution mode

The shipped specs set `quantization: fp8` with `dtype: bfloat16`. vLLM converts the original checkpoint weights to FP8 when loading; `dtype` controls activation and compute precision. Setting `dtype: float16` would save no memory over bf16.

The A100 has no native FP8 support, so this runs as weight-only FP8: weight memory is roughly half, about 9 GB per 8B model instead of about 16 GB, with a modest speed gain. The two shipped FP8 specs use about 19 GB together, so they fit on a 40 GB A100 with room for KV cache; the runner uses concurrent mode with both servers up and requests interleaved.

A bf16 pair uses about 32 GB and needs sequential mode on a 40 GB A100: one server at a time using the whole GPU in the same Colab session. To compare full precision, copy a spec, set `quantization: null`, set `weights_gb` to about 16.5, and give it a name without `-fp8`. The registry's `fits_concurrently` check determines whether both specs fit. Print a server command with `gate models serve-cmd <name> --port 8001`.

## Secrets

Never put Hugging Face tokens in spec files or the repository. Use the Colab secret `HF_TOKEN`.
