"""
AI Wallet Guard v5 — Self-Learning Tax Classifier (AI 自學習稅務分類)

How it learns:
1. Bootstrapped with a small seed set derived from the rule table.
2. Every transaction is classified by a Naive Bayes model over the text
   "merchant + category + reason", with a confidence score.
3. Low-confidence predictions are flagged for human review in the dashboard.
4. When a human corrects/confirms a label, it is stored in `tax_labels`
   with source='human' and the model retrains instantly.
5. Human labels are weighted 3x — corrections dominate the seed rules,
   so the classifier genuinely adapts to YOUR vendors over time.

Pure-Python Naive Bayes (Laplace smoothing) — no sklearn dependency,
retrains in milliseconds at demo scale.
"""

import math
import re
from collections import defaultdict, Counter
from typing import Dict, Any, List, Tuple

from storage import query_df, execute, now_iso

CONFIDENCE_THRESHOLD = 0.55   # below this → "needs human review"
HUMAN_LABEL_WEIGHT = 3        # human corrections count 3x vs seed examples

TAX_CATEGORIES = [
    "Digital services / data licensing",
    "Cloud infrastructure (IaaS)",
    "Digital services / content generation",
    "Software / SaaS subscription",
    "Metered digital services",
    "Digital asset transfer (flagged)",
    "Funds transfer (flagged)",
    "Professional services",
    "Unclassified (manual review)",
]

# Small seed set: (text, label). Deliberately imperfect — the point is
# that human feedback improves on it.
SEED_EXAMPLES: List[Tuple[str, str]] = [
    ("paiddataapi data api buy paid search result", "Digital services / data licensing"),
    ("dataset marketplace data api purchase records", "Digital services / data licensing"),
    ("news api data api fetch articles", "Digital services / data licensing"),
    ("gpu-rent-node compute rent gpu seconds", "Cloud infrastructure (IaaS)"),
    ("cloud vm compute virtual machine hours", "Cloud infrastructure (IaaS)"),
    ("serverless compute function execution", "Cloud infrastructure (IaaS)"),
    ("imagegenapi image generation product thumbnail", "Digital services / content generation"),
    ("video render image generation media processing", "Digital services / content generation"),
    ("vectordb-pro database vector storage monthly", "Software / SaaS subscription"),
    ("saas tool software license seat", "Software / SaaS subscription"),
    ("gpu-rent-node stream per second metered usage", "Metered digital services"),
    ("stream metered usage units", "Metered digital services"),
    ("crypto wallet transfer tokens", "Digital asset transfer (flagged)"),
    ("external transfer send funds account", "Funds transfer (flagged)"),
    ("consultant invoice professional advisory", "Professional services"),
    ("unknownvendor unknown unclear purpose", "Unclassified (manual review)"),
]

_TOKEN_RE = re.compile(r"[a-z0-9]+")

def tokenize(text: str) -> List[str]:
    return _TOKEN_RE.findall(text.lower())


# ---------------------------------------------------------------------------
# Training data store
# ---------------------------------------------------------------------------

def bootstrap_seed_labels():
    """Insert the seed examples once."""
    count = query_df("SELECT COUNT(*) AS c FROM tax_labels").iloc[0]["c"]
    if int(count) == 0:
        for text, label in SEED_EXAMPLES:
            execute("INSERT INTO tax_labels (text, label, source, created_at) VALUES (?, ?, 'seed', ?)",
                    (text, label, now_iso()))

def add_human_label(merchant: str, category: str, reason: str, label: str) -> int:
    """Human feedback: store a corrected/confirmed label and retrain."""
    if label not in TAX_CATEGORIES:
        raise ValueError(f"label must be one of {TAX_CATEGORIES}")
    text = f"{merchant} {category} {reason}".strip()
    label_id = execute("INSERT INTO tax_labels (text, label, source, created_at) VALUES (?, ?, 'human', ?)",
                       (text, label, now_iso()))
    _invalidate_model()
    return label_id

def training_stats() -> Dict[str, int]:
    rows = query_df("SELECT source, COUNT(*) AS c FROM tax_labels GROUP BY source")
    stats = dict(zip(rows["source"], rows["c"])) if not rows.empty else {}
    return {"seed": int(stats.get("seed", 0)), "human": int(stats.get("human", 0))}


# ---------------------------------------------------------------------------
# Naive Bayes model
# ---------------------------------------------------------------------------

class NaiveBayes:
    def __init__(self):
        self.class_counts: Counter = Counter()
        self.word_counts: Dict[str, Counter] = defaultdict(Counter)
        self.vocab: set = set()
        self.total = 0

    def fit(self, examples: List[Tuple[str, str, int]]):
        """examples: (text, label, weight)"""
        for text, label, weight in examples:
            self.class_counts[label] += weight
            self.total += weight
            for token in tokenize(text):
                self.word_counts[label][token] += weight
                self.vocab.add(token)

    def predict(self, text: str) -> Tuple[str, float]:
        tokens = tokenize(text)
        if not self.class_counts:
            return "Unclassified (manual review)", 0.0
        vocab_size = max(len(self.vocab), 1)
        log_scores = {}
        for label, class_count in self.class_counts.items():
            score = math.log(class_count / self.total)
            label_word_total = sum(self.word_counts[label].values())
            for token in tokens:
                token_count = self.word_counts[label].get(token, 0)
                score += math.log((token_count + 1) / (label_word_total + vocab_size))
            log_scores[label] = score
        # softmax over log scores → confidence
        max_log = max(log_scores.values())
        exp_scores = {k: math.exp(v - max_log) for k, v in log_scores.items()}
        denom = sum(exp_scores.values())
        best = max(exp_scores, key=exp_scores.get)
        return best, exp_scores[best] / denom


_model: NaiveBayes = None  # cached; invalidated when new labels arrive

def _invalidate_model():
    global _model
    _model = None

def invalidate_model():
    """Public: call after demo resets so the cached model retrains from the fresh label table."""
    _invalidate_model()

def get_model() -> NaiveBayes:
    global _model
    if _model is None:
        bootstrap_seed_labels()
        rows = query_df("SELECT text, label, source FROM tax_labels")
        examples = [
            (r["text"], r["label"], HUMAN_LABEL_WEIGHT if r["source"] == "human" else 1)
            for _, r in rows.iterrows()
        ]
        _model = NaiveBayes()
        _model.fit(examples)
    return _model


# ---------------------------------------------------------------------------
# Public classification API
# ---------------------------------------------------------------------------

def classify(merchant: str, category: str, reason: str) -> Dict[str, Any]:
    text = f"{merchant} {category} {reason}".strip()
    label, confidence = get_model().predict(text)
    return {
        "text": text,
        "label": label,
        "confidence": round(confidence, 3),
        "needs_review": confidence < CONFIDENCE_THRESHOLD or label.endswith("(flagged)")
                        or label == "Unclassified (manual review)",
        "model": "naive_bayes_v1",
        "training_examples": training_stats(),
    }
