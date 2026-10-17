import json, sys
import cadquery as cq
import builder, validation as V

part, errors = V.parse(json.load(open(sys.argv[1])))
if part is None:
    sys.exit(print("SCHEMA ERRORS:", json.dumps(errors, indent=1)))

errors = V.plausibility(part)
print("REVIEW LIST:", json.dumps(V.review_list(part), indent=1, ensure_ascii=False))
if errors:
    sys.exit(print("PLAUSIBILITY ERRORS:", json.dumps(errors, indent=1)))

model = builder.build(part)
print("MODEL CHECK:", json.dumps(V.check_model(model, part), indent=1))

out = sys.argv[1].replace(".json", ".step")
cq.exporters.export(model, out)
print("STEP:", out, V.check_step(out))