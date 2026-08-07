"""Number-consistency gate — anti-hallucination for experiment papers.

Borrowed from AI-Scientist's Results-node constraint: every number that appears
in the draft must be traceable to the raw experiment results under ``DATA_ROOT``.
The single biggest failure mode of an "experiment -> paper" writer is prose that
quotes a metric the experiment never produced (a hallucinated or fat-fingered
number). This module reads the draft, loads the ground-truth results store, and
flags any figure in the text that looks like it is citing a metric but disagrees
with the recorded value.

Design priority (matches ``data/README.md``): *prefer a miss over a false alarm.*
A number is only ever flagged when it can be tied to a specific result key, so
years, layer counts, epoch counts and other incidental integers are left alone.

Standard library only (re, os, json, csv). No third-party dependencies.
"""

import csv
import json
import os
import re

# ── Tunables ────────────────────────────────────────────────────────────
# Max interstitial characters between a metric phrase and the number claimed
# for it. Two directions, deliberately asymmetric:
#   AFTER  — "accuracy of 91.8%" / "loss settled at 0.12": connectives allowed.
#   BEFORE — "91.8% accuracy" / "4.2 wall clock hours": must be tight adjacency,
#            otherwise a number belonging to the *previous* metric leaks across a
#            clause boundary ("...accuracy of 82.3%, and the train loss ...").
MAX_GAP_AFTER = 18
MAX_GAP_BEFORE = 8
# Scale factors tried when comparing a claimed value to a stored one. Covers the
# percent<->fraction gap (0.823 vs 82.3) without special-casing every unit.
_SCALES = (1.0, 0.01, 100.0)

# Trailing key segments that carry no metric meaning (AI-Scientist wrappers +
# common statistics). Stripped from the tail when deriving a metric phrase.
_GENERIC_TAIL = {
    "means", "mean", "value", "val", "avg", "average", "median",
    "n", "count", "std", "stddev", "stderr", "se", "sem", "var", "variance",
    "min", "max", "sum",
}
# Filenames that name a container rather than a run. When a results file is
# called one of these, the run identity lives in its parent directory name.
_GENERIC_STEMS = {
    "final_info", "final", "info", "results", "result", "metrics", "metric",
    "summary", "output", "outputs", "data", "eval", "evaluation", "scores",
    "all_results", "test_results", "train_results",
}

# Words that are structural, not descriptive; a phrase made only of these is
# too generic to match against (avoids matching bare "run"/"result" in prose).
_STRUCTURAL_WORDS = {
    "run", "runs", "experiment", "experiments", "metric", "metrics",
    "result", "results", "data", "seed", "seeds", "trial", "trials",
}

# Number token: optional sign, thousands-grouped / decimal / integer mantissa,
# optional exponent, optional %/x/× unit suffix. Leading lookbehind keeps us out
# of identifiers ("A100", "run0", "v1.2") and already-consumed decimal tails.
_NUM_RE = re.compile(
    r"(?<![\w.])"
    r"([-+]?(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\.\d+|\d+)(?:[eE][-+]?\d+)?)"
    r"(%|×|(?<=\d)[xX](?![A-Za-z0-9]))?"
)


# ── Small numeric helpers ────────────────────────────────────────────────
def _is_number(x) -> bool:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return False
    return x == x and x not in (float("inf"), float("-inf"))


def _try_float(s):
    """Best-effort coerce a string/number to float; None if it isn't numeric."""
    if s is None:
        return None
    if isinstance(s, bool):
        return None
    if isinstance(s, (int, float)):
        return float(s) if _is_number(s) else None
    t = str(s).strip().replace(",", "")
    if not t:
        return None
    if t.endswith("%"):
        t = t[:-1].strip()
    try:
        v = float(t)
    except ValueError:
        return None
    return v if _is_number(v) else None


def _rel_diff(a: float, b: float) -> float:
    """Symmetric relative difference, stable near zero."""
    denom = max(abs(a), abs(b), 1e-12)
    return abs(a - b) / denom


def _values_agree(claimed: float, expected: float, tol: float) -> bool:
    """True if `claimed` equals `expected` under any plausible unit scaling."""
    return any(_rel_diff(claimed * s, expected) <= tol for s in _SCALES)


def _best_scaled_absdiff(claimed: float, expected: float) -> float:
    return min(abs(claimed * s - expected) for s in _SCALES)


def _plausible_scale(claimed: float, expected: float) -> bool:
    """Guard against flagging a number that is wildly off-magnitude even after
    percent/fraction scaling (i.e. clearly not an attempt at this metric)."""
    if expected == 0:
        return True
    best = min((claimed * s for s in _SCALES), key=lambda v: abs(v - expected))
    if best == 0:
        return abs(expected) <= 1e-6
    ratio = abs(best) / abs(expected)
    return 1e-3 <= ratio <= 1e3


def _fmt(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (int, float)):
        return "%g" % v
    return str(v)


# ── (1) Results store ─────────────────────────────────────────────────────
def _flatten(obj, prefix: str, store: dict) -> None:
    """Recursively collect numeric leaves into `store` with dot-joined keys."""
    if isinstance(obj, dict):
        # AI-Scientist metric object: {"means": v, "stderr": s, "n": k}.
        mean_val = None
        for mk in ("means", "mean"):
            if mk in obj and _is_number(obj[mk]):
                mean_val = float(obj[mk])
                break
        if mean_val is not None and prefix:
            store[prefix] = mean_val
            for k, v in obj.items():
                if k in ("means", "mean"):
                    continue
                _flatten(v, f"{prefix}.{k}", store)
            return
        for k, v in obj.items():
            nk = f"{prefix}.{k}" if prefix else str(k)
            _flatten(v, nk, store)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            nk = f"{prefix}.{i}" if prefix else str(i)
            _flatten(v, nk, store)
    else:
        if _is_number(obj):
            store[prefix] = float(obj)
        elif isinstance(obj, str):
            v = _try_float(obj)
            if v is not None:
                store[prefix] = v
            else:
                # I12: keep textual metadata (run_name, description, hardware,
                # dataset, hyperparameters) so the context pack / evidence mining
                # can describe WHAT was run, not just the numbers. Non-numeric
                # strings are stored with a ":" marker to distinguish text from
                # numeric metrics for downstream table rendering.
                store[prefix] = ":" + obj


def _load_csv(path: str, stem: str, store: dict) -> None:
    with open(path, "r", newline="", encoding="utf-8", errors="replace") as f:
        rows = [r for r in csv.reader(f) if r]
    if not rows:
        return
    header = rows[0]
    hlower = [h.strip().lower() for h in header]
    data = rows[1:]

    name_cols = [i for i, h in enumerate(hlower)
                 if h in ("name", "metric", "key", "metric_name")]
    val_cols = [i for i, h in enumerate(hlower)
                if h in ("value", "means", "mean", "result", "score")]

    # Long / key-value format.
    if name_cols and val_cols:
        ni, vi = name_cols[0], val_cols[0]
        for r in data:
            if len(r) <= max(ni, vi):
                continue
            name = r[ni].strip()
            v = _try_float(r[vi])
            if name and v is not None:
                store[f"{stem}.{name}"] = v
        return

    if not data:
        return

    # Wide format. If the first column is non-numeric it is a row label.
    first_col_numeric = all(_try_float(r[0]) is not None for r in data if r)
    for ri, r in enumerate(data):
        start = 0 if first_col_numeric else 1
        label = None if first_col_numeric else r[0].strip()
        for ci in range(start, len(r)):
            v = _try_float(r[ci])
            if v is None:
                continue
            col = header[ci].strip() if ci < len(header) and header[ci].strip() else f"col{ci}"
            if label:
                key = f"{stem}.{label}.{col}"
            elif len(data) > 1:
                key = f"{stem}.{col}.{ri}"
            else:
                key = f"{stem}.{col}"
            store[key] = v


def load_results_store(data_root: str) -> dict:
    """Scan `data_root` for *.json / *.csv and flatten into {metric: float}.

    JSON nested dicts are dot-joined; AI-Scientist ``means`` objects register
    under the metric's own path. CSV long tables (name/value) and wide tables
    are both handled. Unparseable files are skipped silently. All values float.
    """
    store: dict = {}
    root = str(data_root)
    if not os.path.isdir(root):
        return store
    for dirpath, _dirs, filenames in os.walk(root):
        for fn in filenames:
            # I11: skip example/template files so illustrative data is never
            # treated as real experimental evidence.
            if ".example." in fn or fn.endswith(".example") or fn.startswith("_template"):
                continue
            ext = os.path.splitext(fn)[1].lower()
            path = os.path.join(dirpath, fn)
            # Key prefix must uniquely identify the run, or one run silently
            # overwrites another. Two layouts are both valid and both appear in
            # the wild:
            #   results/run_0/final_info.json  -> prefix "run_0"  (dir is the run)
            #   results/run_0.json             -> prefix "run_0"  (file is the run)
            # I11 fixed the first (filename "final_info" collides across runs).
            # The second collides the other way: every file under results/ would
            # take the parent's name. So prefer the directory name only when the
            # filename is a generic container name.
            stem = os.path.splitext(fn)[0]
            parent = os.path.basename(os.path.dirname(path))
            if stem.lower() in _GENERIC_STEMS and parent and parent != ".":
                prefix = parent
            else:
                prefix = stem or parent
            try:
                if ext == ".json":
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        data = json.load(f)
                    _flatten(data, prefix, store)
                elif ext == ".csv":
                    _load_csv(path, prefix, store)
            except Exception:
                # Robust by contract: a broken file must not sink the whole scan.
                continue
    return store


# ── (2) Number extraction ─────────────────────────────────────────────────
def _extract(text: str) -> list:
    """Internal: like extract_numbers but also carries span + unit flags."""
    out = []
    for m in _NUM_RE.finditer(text):
        mantissa = m.group(1)
        suffix = m.group(2) or ""
        try:
            value = float(mantissa.replace(",", ""))
        except ValueError:
            continue
        if not _is_number(value):
            continue
        start, end = m.start(), m.end()
        ctx = text[max(0, start - 40): end + 40]
        out.append({
            "raw": m.group(0).strip(),
            "value": value,                       # "82.3%" -> 82.3
            "context": ctx,
            "start": start,
            "end": end,
            "percent": suffix == "%",
            "multiplier": suffix not in ("", "%"),
        })
    return out


def extract_numbers(text: str) -> list:
    """Return every number in `text` as {"raw", "value", "context"}.

    Handles percentages ("82.3%" -> 82.3), decimals, integers, multipliers
    ("3x" / "3×"), thousands separators and scientific notation. Context is the
    surrounding ±40 characters.
    """
    return [{"raw": n["raw"], "value": n["value"], "context": n["context"]}
            for n in _extract(text)]


# ── (3) Consistency check ─────────────────────────────────────────────────
def _metric_phrase(key: str):
    """Derive a searchable (label, compiled-regex) from a result key.

    Uses the last non-generic key segment as the metric name, e.g.
    ``run0.metrics.test_accuracy.means`` -> "test accuracy". Returns
    (None, None) when the key is too generic/structural to match safely.
    """
    segs = [s for s in key.split(".") if s]
    while segs and segs[-1].lower() in _GENERIC_TAIL:
        segs.pop()
    if not segs:
        return None, None
    leaf = segs[-1]
    words = [w for w in re.split(r"[\s_\-]+", leaf) if w]
    words = [w for w in words if not re.fullmatch(r"\d+", w)]
    words = [w for w in words if w.lower() not in _STRUCTURAL_WORDS]
    if not words:
        return None, None
    label = " ".join(w.lower() for w in words)
    pattern = r"\b" + r"[\s_\-]+".join(re.escape(w) for w in words) + r"\b"
    return label, re.compile(pattern, re.IGNORECASE)


def check_numbers_against_results(text: str, results: dict, tol: float = 0.01) -> dict:
    """Cross-check numbers in `text` against `results`.

    A number is only judged when it sits adjacent (<= MAX_GAP chars) to a metric
    phrase drawn from a result key. If it matches any same-named metric under a
    plausible unit scaling it passes; otherwise it is a mismatch reported against
    the closest recorded value.

    Returns {"mismatches": [{"claimed", "expected", "metric", "context"}],
             "checked": int, "ok": bool}.
    """
    nums = _extract(text)
    mismatches: list = []
    checked = 0

    if not nums or not results:
        return {"mismatches": mismatches, "checked": checked, "ok": True}

    # Group result keys by their metric phrase so same-named runs share a probe.
    phrase_map: dict = {}
    for key, val in results.items():
        if not _is_number(val):
            continue
        label, regex = _metric_phrase(key)
        if label is None:
            continue
        entry = phrase_map.setdefault(label, {"regex": regex, "items": []})
        entry["items"].append((key, float(val)))

    # Bind each metric-phrase occurrence to its nearest eligible number, then
    # resolve globally tightest-first so a number is claimed by the phrase it is
    # closest to (e.g. "4.2 wall clock hours" takes 4.2, not the later "1 GPU").
    candidates = []  # (gap, number_index, label)
    for label, entry in phrase_map.items():
        for pm in entry["regex"].finditer(text):
            ps, pe = pm.start(), pm.end()
            best_idx, best_gap = None, None
            for i, n in enumerate(nums):
                if n["end"] <= ps:                       # number BEFORE phrase
                    gap = ps - n["end"]
                    if gap > MAX_GAP_BEFORE:
                        continue
                elif n["start"] >= pe:                    # number AFTER phrase
                    gap = n["start"] - pe
                    if gap > MAX_GAP_AFTER:
                        continue
                else:
                    gap = 0
                if best_gap is None or gap < best_gap:
                    best_gap, best_idx = gap, i
            if best_idx is not None:
                candidates.append((best_gap, best_idx, label))

    candidates.sort(key=lambda c: c[0])  # tightest adjacency wins the number
    claimed_idx = set()
    for _gap, idx, label in candidates:
        if idx in claimed_idx:
            continue
        claimed_idx.add(idx)
        items = phrase_map[label]["items"]
        n = nums[idx]
        checked += 1
        claimed = n["value"]

        if any(_values_agree(claimed, v, tol) for _, v in items):
            continue  # consistent with at least one same-named metric

        # Not matched: report against the closest recorded value.
        metric_key, expected = min(
            items, key=lambda kv: _best_scaled_absdiff(claimed, kv[1]))
        if not _plausible_scale(claimed, expected):
            continue  # off by orders of magnitude -> not an attempt at this metric
        mismatches.append({
            "claimed": n["raw"],
            "expected": expected,
            "metric": metric_key,
            "context": n["context"].strip(),
        })

    return {"mismatches": mismatches, "checked": checked, "ok": not mismatches}


# ── (4) Gate entry point ───────────────────────────────────────────────────
def run_number_gate(draft_path: str, data_root: str, tol: float = 0.01):
    """Read a draft, load results, cross-check. Returns (passed, messages).

    Missing files are handled gracefully (no exception). Each mismatch message
    reads: ``[NUMBER-MISMATCH] 正文 '82.3%' 但 results['run0.accuracy']=0.918``.
    """
    if not os.path.isfile(draft_path):
        return False, [f"[NUMBER-GATE] draft file not found: {draft_path}; cannot verify numbers."]

    try:
        with open(draft_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        return False, [f"[NUMBER-GATE] could not read draft {draft_path}: {e}"]

    results = load_results_store(data_root)
    if not results:
        # Fail closed: experiment papers must be grounded in real results. With no
        # results store, every number in the draft is unverifiable — that is a
        # blocking condition, not a "skip the check" condition.
        return False, [f"[NUMBER-GATE] no results found under {data_root}; every number in the draft is unverifiable. Provide data/ results before drafting."]

    report = check_numbers_against_results(text, results, tol=tol)
    if report["ok"]:
        return True, [
            f"[NUMBER-GATE] OK: {report['checked']} number(s) cross-checked "
            f"against {len(results)} result value(s), no mismatch."
        ]

    messages = [
        f"[NUMBER-MISMATCH] 正文 '{m['claimed']}' 但 "
        f"results['{m['metric']}']={_fmt(m['expected'])}"
        for m in report["mismatches"]
    ]
    return False, messages


# ── Self-test (no real files needed) ───────────────────────────────────────
if __name__ == "__main__":
    results = {
        "run0.test_accuracy": 0.918,
        "run0.train_loss": 0.1204,
        "run0.wall_clock_hours": 4.2,
        "run1.test_accuracy": 0.9231,
    }

    good_text = (
        "In 2021 we trained a 12-layer Transformer for 100 epochs. "
        "The baseline reaches a test accuracy of 91.8% and a train loss of 0.12, "
        "while our improved model attains a test accuracy of 92.31%. "
        "Each run took about 4.2 wall clock hours on 1 GPU."
    )
    bad_text = (
        "In 2021, the baseline achieved a test accuracy of 82.3%, "
        "and the train loss settled at 0.95 after 100 epochs."
    )

    print("results store:", results)
    print()

    ok_report = check_numbers_against_results(good_text, results)
    print("[GOOD TEXT] checked=%d ok=%s mismatches=%d"
          % (ok_report["checked"], ok_report["ok"], len(ok_report["mismatches"])))
    for m in ok_report["mismatches"]:
        print("   unexpected:", m)
    assert ok_report["ok"], "correct numbers / year / layers must NOT be flagged"

    bad_report = check_numbers_against_results(bad_text, results)
    print("[BAD TEXT]  checked=%d ok=%s mismatches=%d"
          % (bad_report["checked"], bad_report["ok"], len(bad_report["mismatches"])))
    for m in bad_report["mismatches"]:
        print("   [NUMBER-MISMATCH] 正文 '%s' 但 results['%s']=%s"
              % (m["claimed"], m["metric"], _fmt(m["expected"])))

    claimed_raw = {m["claimed"] for m in bad_report["mismatches"]}
    assert not bad_report["ok"], "wrong numbers must be flagged"
    assert "82.3%" in claimed_raw, "accuracy mismatch must be caught"
    assert "0.95" in claimed_raw, "loss mismatch must be caught"
    assert not any("2021" in m["claimed"] for m in bad_report["mismatches"]), \
        "the year 2021 must NOT be flagged (false-positive guard)"

    # Run-identity regression: both documented layouts must keep every run
    # distinct. A collision here silently drops a run's numbers from the store,
    # which then reads as "the draft cited a number we have no record of".
    import tempfile as _tf
    for layout in ("file", "dir"):
        _root = _tf.mkdtemp()
        _res = os.path.join(_root, "results")
        os.makedirs(_res)
        for _name, _val in (("run_0_baseline", 0.90), ("run_1_ours", 0.95)):
            _payload = json.dumps({"metrics": {"test_accuracy": {"means": _val}}})
            if layout == "file":
                _path = os.path.join(_res, f"{_name}.json")
            else:
                os.makedirs(os.path.join(_res, _name))
                _path = os.path.join(_res, _name, "final_info.json")
            with open(_path, "w", encoding="utf-8") as _f:
                _f.write(_payload)
        _store = load_results_store(_root)
        assert len(_store) == 2, f"layout '{layout}' collapsed two runs into {_store}"
        assert any("run_0_baseline" in k for k in _store), f"layout '{layout}': {_store}"
        assert any("run_1_ours" in k for k in _store), f"layout '{layout}': {_store}"
    print()
    print("run-identity check: both layouts keep runs distinct")

    nums = extract_numbers("throughput hit 3x at 1.2e3 tokens/s, i.e. 250,000 total")
    print()
    print("extract_numbers demo:", [(n["raw"], n["value"]) for n in nums])

    print()
    print("SELF-TEST PASSED: catches mismatches, ignores correct numbers/years.")
