import sys
sys.path.insert(0, "../analysis-0.37.0/repro/products")
from helpers import fresh_jail
from app.products import make
jail = fresh_jail("text_overflow")
items = ["Editable CV template for Word and Pages", "Matching cover letter and reference page",
         "A4 and US Letter sizes in every format", "Free fonts and a step by step PDF guide"]
sub = "|".join(items)
print(len(sub), [len(i) for i in items])
for shape in ("landscape", "square", "portrait"):
    m = make.image(jail, f"shop/t-{shape}.png", "", "What is included", sub, "Instant download", shape=shape, layout="text")
    print(m.report)
