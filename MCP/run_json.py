"""Test a part JSON without MCP: python run_json.py examples/x.json [more.json ...]
Prints the review list and model check, writes <name>.step and <name>_review.json
to examples/out/ (or $OUTPUT_DIR). Exit code 1 if any file fails."""
import json
import os
import sys

import cadquery as cq

import builder
import validation as V


def run(path: str, quiet: bool = False) -> bool:
    part, errors = V.parse(json.load(open(path, encoding="utf-8")))
    if part is None:
        print("SCHEMA ERRORS:", json.dumps(errors, indent=1, ensure_ascii=False))
        return False
    errors = V.plausibility(part)
    if errors:
        print("PLAUSIBILITY ERRORS:", json.dumps(errors, indent=1, ensure_ascii=False))
        return False
    notes = []
    model = builder.build(part, notes)
    check = V.check_model(model, part, notes)
    review = V.review_list(part, notes)
    if not quiet:
        print("REVIEW LIST:", json.dumps(review, indent=1, ensure_ascii=False))
        print("MODEL CHECK:", json.dumps(check, indent=1, ensure_ascii=False))
    out_dir = os.environ.get("OUTPUT_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples", "out"))
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.join(out_dir, os.path.splitext(os.path.basename(path))[0])
    out = stem + ".step"
    cq.exporters.export(model, out)
    step = V.check_step(out)
    with open(stem + "_review.json", "w", encoding="utf-8") as fh:
        json.dump({"part": part.name, "step_file": os.path.basename(out), "validated": check["ok"],
                   "model_check": check, "step_check": step, "review_list": review},
                  fh, indent=2, ensure_ascii=False)
    ok = check["ok"] and step["reimport_ok"]
    print(f"{'OK  ' if ok else 'FAIL'} {path}: volume {check['volume']}, size {check['size_xyz']}, "
          f"failed checks {[k for k, v in check['checks'].items() if not v]}, "
          f"review items {len(review)}, STEP {step}")
    return ok


if __name__ == "__main__":
    quiet = "-q" in sys.argv
    files = [a for a in sys.argv[1:] if a != "-q"]
    results = [run(f, quiet) for f in files]
    sys.exit(0 if all(results) else 1)
