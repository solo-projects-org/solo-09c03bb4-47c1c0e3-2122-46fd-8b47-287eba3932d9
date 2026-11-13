"""Fixed human gene annotation release used by the local source.

A small, frozen subset of Ensembl/GRCh38-style annotations keyed by ENSG
identifier *without* version suffix.  In a real deployment this would be a
full GTF-derived table; here it is deliberately small but realistic so the
whole stack is self-contained and demonstrable.
"""

ANNOTATION_RELEASE = "GRCh38.p14 (Ensembl 112) - local demo subset"

ANNOTATIONS: dict[str, dict[str, str]] = {
    "ENSG00000141510": {"symbol": "TP53", "chromosome": "17"},
    "ENSG00000012048": {"symbol": "BRCA1", "chromosome": "17"},
    "ENSG00000139618": {"symbol": "BRCA2", "chromosome": "13"},
    "ENSG00000146648": {"symbol": "EGFR", "chromosome": "7"},
    "ENSG00000133703": {"symbol": "KRAS", "chromosome": "12"},
    "ENSG00000130203": {"symbol": "APOE", "chromosome": "19"},
    "ENSG00000001626": {"symbol": "CFTR", "chromosome": "7"},
    "ENSG00000198947": {"symbol": "DMD", "chromosome": "X"},
    "ENSG00000197386": {"symbol": "HTT", "chromosome": "4"},
    "ENSG00000254647": {"symbol": "INS", "chromosome": "11"},
    "ENSG00000232810": {"symbol": "TNF", "chromosome": "6"},
    "ENSG00000136244": {"symbol": "IL6", "chromosome": "7"},
    "ENSG00000112715": {"symbol": "VEGFA", "chromosome": "6"},
    "ENSG00000136997": {"symbol": "MYC", "chromosome": "8"},
    "ENSG00000171862": {"symbol": "PTEN", "chromosome": "10"},
    "ENSG00000111640": {"symbol": "GAPDH", "chromosome": "12"},
    "ENSG00000075624": {"symbol": "ACTB", "chromosome": "7"},
    "ENSG00000163631": {"symbol": "ALB", "chromosome": "4"},
    "ENSG00000155657": {"symbol": "TTN", "chromosome": "2"},
    "ENSG00000244734": {"symbol": "HBB", "chromosome": "11"},
    "ENSG00000198763": {"symbol": "MT-ND1", "chromosome": "MT"},
}
