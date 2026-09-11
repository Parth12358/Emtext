"""Strip training state out of the emotion2vec checkpoint.

    python -m eval.slim_ser_ckpt            # build models/emotion2vec_plus_base_slim/
    python -m eval.slim_ser_ckpt --check    # report sizes, write nothing

The problem this solves, measured rather than assumed:

The server's resident set is ~3.0 GB, and **82% of it is the SER model load**
(+2,496 MB on top of a 552 MB process). The live model is only 358 MB. The gap
has two causes that multiply:

1. **The checkpoint is two-thirds training baggage.** ModelScope ships the full
   pretraining state, not an inference export:

       model                  355.4 MB   <- the only part inference uses
       last_optimizer_state   710.9 MB   <- Adam momentum + variance, exactly 2x
       everything else          ~0 MB
       -------------------------------
       total                 1066.3 MB   (file on disk: 1066.4 MB)

2. **FunASR deep-copies the whole thing.** `load_pretrained_model.py` does
   `torch.load(path)` and then `copy.deepcopy(ori_state)` before pulling out the
   `model` key -- so 1,066 MB is materialised twice. Peak is
   1066 + 1066 + 355 = 2,487 MB, which matches the measured +2,496 MB.

And the peak is permanent: `del` + `gc.collect()` frees nothing measurable,
because the Windows allocator does not return the pages. Peak allocation *is*
steady-state RSS here, so the only lever is allocating less in the first place.

Dropping `last_optimizer_state` shrinks both the load and the copy, which is why
this saves ~1.4 GB rather than the 711 MB you would expect from the file size.

Safe because FunASR reads exactly one key from the checkpoint --
`src_state["model"]` -- and takes everything else (architecture, frontend, label
set) from `config.yaml` and `configuration.json` in the same directory, which are
copied verbatim. Nothing is written to the ModelScope cache; the original stays
put as the fallback.
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

MB = 1024 * 1024

# Where ModelScope puts the model. `master` is a branch, not a commit -- upstream
# publishes no immutable ref for this model (see TODO.md), so this is what it is.
SRC_DIR = (Path(os.path.expanduser("~")) / ".cache" / "modelscope" / "models"
           / "iic--emotion2vec_plus_base" / "snapshots" / "master")
DST_DIR = Path(__file__).resolve().parent.parent / "models" / "emotion2vec_plus_base_slim"

# Small files FunASR needs alongside the weights. config.yaml carries the
# architecture, configuration.json the pipeline wiring, tokens.txt the labels.
SIDECARS = ("config.yaml", "configuration.json", "tokens.txt")

# Keys worth keeping. Only "model" is read by the loader; "cfg" and "args" are
# kept because they are free (no tensors) and make the file self-describing if
# anyone inspects it later.
KEEP = ("model", "cfg", "args")


def _tensor_bytes(obj) -> int:
    """Recursive tensor byte count -- optimizer state is nested several levels."""
    if hasattr(obj, "numel") and hasattr(obj, "element_size"):
        return obj.numel() * obj.element_size()
    if isinstance(obj, dict):
        return sum(_tensor_bytes(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return sum(_tensor_bytes(v) for v in obj)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="report what would be dropped, write nothing")
    ap.add_argument("--src", type=Path, default=SRC_DIR)
    ap.add_argument("--dst", type=Path, default=DST_DIR)
    args = ap.parse_args()

    src_pt = args.src / "model.pt"
    if not src_pt.exists():
        raise SystemExit(
            f"no checkpoint at {src_pt}\n"
            "Run the server once (or `python -m server.ser <wav>`) to populate the\n"
            "ModelScope cache, then re-run this."
        )

    import torch  # deferred: this is a one-shot tool, not on the server's path

    before = src_pt.stat().st_size
    print(f"source     {src_pt}")
    print(f"           {before / MB:.1f} MB")
    print("loading (this briefly needs ~1 GB) ...", flush=True)
    state = torch.load(src_pt, map_location="cpu")

    print()
    print(f"  {'key':24} {'MB':>9}")
    print(f"  {'-' * 24} {'-' * 9}")
    dropped = 0
    for key in state:
        mb = _tensor_bytes(state[key]) / MB
        mark = "keep" if key in KEEP else "DROP"
        if key not in KEEP:
            dropped += mb
        print(f"  {key:24} {mb:9.1f}  {mark}")
    print()

    slim = {k: state[k] for k in KEEP if k in state}
    if "model" not in slim:
        raise SystemExit("checkpoint has no 'model' key -- refusing to write a slim "
                         "file that FunASR could not load")

    if args.check:
        print(f"--check: would drop {dropped:.1f} MB, writing nothing")
        return

    args.dst.mkdir(parents=True, exist_ok=True)
    dst_pt = args.dst / "model.pt"
    torch.save(slim, dst_pt)

    for name in SIDECARS:
        s = args.src / name
        if s.exists():
            shutil.copy2(s, args.dst / name)
            print(f"copied     {name}")
        else:
            print(f"missing    {name}  (FunASR may still cope; check the load)")

    after = dst_pt.stat().st_size
    print()
    print(f"wrote      {dst_pt}")
    print(f"           {before / MB:.1f} MB -> {after / MB:.1f} MB "
          f"({(1 - after / before) * 100:.0f}% smaller)")
    print()
    print("The original cache file is untouched. server/config.py picks this up")
    print("automatically on the next start; unset SER_MODEL_DIR to go back.")
    print()
    print("Verify accuracy is unchanged before trusting it:")
    print("  python -m eval.ser_eval        # expect ~86% average recall")


if __name__ == "__main__":
    main()
