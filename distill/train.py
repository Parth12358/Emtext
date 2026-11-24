"""Fine-tune the small interpreter on a built distillation set (Stage 1, design doc §6).

    .venv-train/Scripts/python -m distill.train --max-steps 30     # timing / memory trial
    .venv-train/Scripts/python -m distill.train                    # full run

Runs in `.venv-train` (torch +xpu, transformers, trl, torch-optimi), NOT the server's
`.venv`: training wants a different torch build, and the server must keep its own. It
reads only `data/distill/<name>/{train,val}.jsonl` and imports nothing from `server/`,
so it also runs on a rented box with just this file and the data.

Why each non-default choice
---------------------------
- **Full fine-tune in pure bf16 with Kahan-summed AdamW** (`torch-optimi`). The usual
  recipe (fp32 master weights + AdamW) needs ~16 bytes/param, ~9.6 GB for 0.6B, before
  activations; the B580 has ~10 GB free with a desktop running. Pure bf16 needs ~8
  bytes/param, but rounds away updates smaller than bf16's resolution, which at a 1e-5
  learning rate is most of them. Kahan summation keeps the lost low bits in a bf16
  buffer, recovering fp32-like training for one extra copy of the weights.
- **Thinking off in the template** (`enable_thinking: False` per row): the teacher was
  labelled with `think: false` and the student is served the same way, so it learns
  to answer straight after an empty `<think></think>`.
- **Loss on the completion only**: TRL's prompt/completion format masks the prompt, so
  the ~1,100-token system prompt is context, not a target. `--check-mask` prints what
  carries loss for one row; the design doc (§6.2) says to verify it once.
- **Label smoothing off by default.** It would help calibration (small SFT models get
  overconfident, and `confidence.py` reads the tone/emotion token probabilities, §4), but
  TRL 1.x computes it with an extra full forward pass per step. The confidence Platt fit
  must be redone on the student either way; `--label-smoothing 0.05` turns it on.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")  # Windows: WinError 1314 without it

import torch  # noqa: E402
from datasets import load_dataset  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer, TrainerCallback  # noqa: E402
from trl import SFTConfig, SFTTrainer  # noqa: E402
from trl.kernels import chunked_logprob as _chunked  # noqa: E402

# TRL computes the loss through a chunked LM head (no full 151k-vocab logits), with
# Triton kernels whenever Triton imports. torch-xpu ships triton-xpu, but on Windows
# it must compile a C helper at first use and there is no compiler here ("Failed to
# find C compiler"). TRL's own pure-torch tile path does the same maths; use it.
_chunked._use_triton = lambda device: False

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "Qwen/Qwen3-0.6B"


def _load(data_dir: Path) -> dict:
    files = {s: str(data_dir / f"{s}.jsonl") for s in ("train", "val")}
    ds = load_dataset("json", data_files=files)
    # Per-row template kwargs: TRL passes them to apply_chat_template.
    return ds.map(lambda _: {"chat_template_kwargs": {"enable_thinking": False}})


class _Progress(TrainerCallback):
    """One line per logging step with tokens/s and memory, so a long run never looks hung."""

    def __init__(self) -> None:
        self.t0 = time.perf_counter()
        self.step0 = 0

    def on_train_begin(self, args, state, control, **kw):
        # On --resume global_step starts at the checkpoint's step; time only what ran here.
        self.t0, self.step0 = time.perf_counter(), state.global_step

    def on_log(self, args, state, control, logs=None, **kw):
        if not logs or "loss" not in logs:
            return
        el = time.perf_counter() - self.t0
        rate = el / max(1, state.global_step - self.step0)
        left = rate * (state.max_steps - state.global_step)
        mem = torch.xpu.max_memory_allocated() / 2**30 if torch.xpu.is_available() else 0
        print(f"  step {state.global_step:>5}/{state.max_steps}  loss {logs['loss']:.4f}  "
              f"lr {logs.get('learning_rate', 0):.2e}  {rate:.1f}s/step  "
              f"peak {mem:.1f} GB  eta {left / 60:5.1f}m", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="v2", help="dataset folder under data/distill/")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--out", default=None, help="default: models/student/<model>-<data>")
    ap.add_argument("--epochs", type=float, default=2)
    ap.add_argument("--lr", type=float, default=1e-5)
    # 2 x 16 = an effective batch of 32. 4 x 8 peaked at 7.3 GB, then died at step 18 of
    # the first full run (2026-11-23) with UR_RESULT_ERROR_OUT_OF_RESOURCES on a batch
    # of long rows, with ~10 GB free on the B580.
    ap.add_argument("--batch", type=int, default=2, help="rows per device step")
    ap.add_argument("--accum", type=int, default=16, help="gradient accumulation steps")
    ap.add_argument("--max-length", type=int, default=1536)
    ap.add_argument("--label-smoothing", type=float, default=0.0,
                    help="> 0 costs an extra forward pass per step in TRL 1.x")
    ap.add_argument("--max-steps", type=int, default=-1, help="stop early (a timing trial)")
    # Set it at the START of a run: on --resume, transformers 5 keeps the interval stored
    # in the checkpoint's trainer_state.json and only warns about the mismatch (found
    # 2026-11-24, when --resume --save-steps 25 still saved at 50).
    ap.add_argument("--save-steps", type=int, default=50,
                    help="checkpoint (and val eval) every N steps")
    ap.add_argument("--keep", type=int, default=2, help="checkpoints kept on disk (~4.5 GB each)")
    ap.add_argument("--resume", action="store_true",
                    help="continue from the newest checkpoint in the output folder")
    ap.add_argument("--check-mask", action="store_true",
                    help="print the loss-carrying text of one row and exit")
    args = ap.parse_args()

    data_dir = ROOT / "data" / "distill" / args.data
    out = Path(args.out) if args.out else ROOT / "models" / "student" / \
        f"{args.model.split('/')[-1]}-{args.data}"
    device = "xpu" if torch.xpu.is_available() else "cpu"
    print(f"train: {args.model} on {data_dir.name} -> {out} ({device})", flush=True)

    tok = AutoTokenizer.from_pretrained(args.model)
    ds = _load(data_dir)
    model = AutoModelForCausalLM.from_pretrained(args.model, dtype=torch.bfloat16)

    cfg = SFTConfig(
        output_dir=str(out),
        num_train_epochs=args.epochs,
        max_steps=args.max_steps,
        per_device_train_batch_size=args.batch,
        per_device_eval_batch_size=args.batch,
        gradient_accumulation_steps=args.accum,
        learning_rate=args.lr,
        lr_scheduler_type="cosine",
        warmup_steps=0.03,          # a float is a ratio of total steps (transformers 5)
        weight_decay=0.0,
        bf16=True,
        activation_checkpointing=True,
        max_length=args.max_length,
        completion_only_loss=True,
        **({"label_smoothing_factor": args.label_smoothing} if args.label_smoothing else {}),
        logging_steps=5,
        eval_strategy="steps" if args.max_steps < 0 else "no",
        eval_steps=args.save_steps,
        save_strategy="steps" if args.max_steps < 0 else "no",
        save_steps=args.save_steps,
        save_total_limit=args.keep,
        report_to=[],
        seed=0,
    )
    from optimi import AdamW  # Kahan summation is on by default for bf16 params
    # triton=False: its Triton kernels need a C compiler at runtime, absent on this box.
    opt = AdamW(model.parameters(), lr=args.lr, weight_decay=0.0, triton=False)
    trainer = SFTTrainer(model=model, args=cfg, train_dataset=ds["train"],
                         eval_dataset=ds["val"], processing_class=tok,
                         optimizers=(opt, None), callbacks=[_Progress()])

    if args.check_mask:
        row = trainer.train_dataset[0]
        ids = row["input_ids"]
        mask = [lab != -100 for lab in row["labels"]]  # -100 = no loss
        print(f"tokens {len(ids)}; loss on {sum(mask)}")
        print("PROMPT TAIL:", repr(tok.decode([i for i, m in zip(ids, mask) if not m][-40:])))
        print("LOSS ON:    ", repr(tok.decode([i for i, m in zip(ids, mask) if m])))
        return

    trainer.train(resume_from_checkpoint=True if args.resume else None)
    if args.max_steps < 0:
        trainer.save_model(str(out / "final"))
        tok.save_pretrained(str(out / "final"))
        metrics = trainer.evaluate()
        (out / "final" / "train_summary.json").write_text(
            json.dumps({"args": vars(args), "eval": metrics}, indent=2), encoding="utf-8")
        print(f"saved {out / 'final'}  eval {metrics}", flush=True)


if __name__ == "__main__":
    main()
