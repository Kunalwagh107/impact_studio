import sys
sys.path.insert(0, ".")
from backend import ingest, category_mapping as CM

wb = "TW Impact Study_V2 1 (1).xlsx"
df = ingest.load_table(wb, "Raw_MAT", header_row=1)
sp = ingest.split_by_discriminator(df, "Dataset", "Current MAT", "New MAT")
a, b = sp.a, sp.b
ca = set(a["CATEGORY"].dropna().astype(str).unique())
cb = set(b["CATEGORY"].dropna().astype(str).unique())
print("A cats", len(ca), "B cats", len(cb))
print("B-only:", sorted(cb - ca))
print("A-only:", sorted(ca - cb))
res = CM.enumerate_category_units(a, b, "CATEGORY", None, "Sales Value",
                                 "CATEGORY", None, "Sales Value")
print("res.new_in_b:", [u["label"] for u in res.new_in_b])
print("summary:", {k: v for k, v in res.summary.items() if k in
                  ("n_a", "n_b", "new_in_b", "mapped", "unmapped")})
