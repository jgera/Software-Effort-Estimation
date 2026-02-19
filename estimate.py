#!/usr/bin/env python3
"""
Effort & Cost Estimation Report (Code Scanning)
- Uses Radon (Python maintainability + complexity)
- Uses Lizard (multi-language complexity + function stats)
- Produces executive summary including cost estimates

Usage examples:
  python effort_cost_report.py /path/to/repo
  python effort_cost_report.py /path/to/repo --hourly-rate 60 --currency USD
  python effort_cost_report.py /path/to/repo --include-ext .py .js .java --exclude-dir .git node_modules dist build
  python effort_cost_report.py /path/to/repo --json-out report.json

Notes:
- Radon is applied to Python files only (.py).
- Lizard is applied to the included extensions (default common set).
"""

from __future__ import annotations

import argparse, json, math, os, re, statistics, subprocess, sys
import importlib.util
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def is_installed(package_name):
    """Return True if package is installed, False otherwise."""
    return importlib.util.find_spec(package_name) is not None


def install_package(package_name):
    """Install a package using pip."""
    print(f"Installing {package_name}...")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", package_name]
    )
    print(f"{package_name} installed successfully.\n")


def ensure_package(package_name):
    """Ensure a package is installed."""
    if is_installed(package_name):
        #print(f"{package_name} is already installed.")
        pass
    else:
        print(f"{package_name} is NOT installed.")
        install_package(package_name)



packages = ["radon", "lizard"]

for pkg in packages:
	ensure_package(pkg)




# ----------------------------
# Data models
# ----------------------------

@dataclass
class RadonCCFileResult:
    file: str
    avg_cc: float
    max_cc: int
    blocks: int  # number of analyzed blocks/functions/classes


@dataclass
class RadonMIFileResult:
    file: str
    mi: float


@dataclass
class LizardFunctionResult:
    file: str
    function: str
    nloc: int
    cc: int
    params: int
    length: int  # lines or length metric from lizard output


@dataclass
class AggregateMetrics:
    scanned_files: int
    scanned_loc: int

    # Radon (Python-only)
    python_files: int
    radon_cc_files: int
    radon_mi_files: int
    radon_avg_cc: Optional[float]
    radon_max_cc: Optional[int]
    radon_avg_mi: Optional[float]

    # Lizard (multi-language)
    lizard_files: int
    lizard_functions: int
    lizard_avg_cc: Optional[float]
    lizard_p90_cc: Optional[float]
    lizard_max_cc: Optional[int]
    lizard_avg_nloc: Optional[float]
    hotspots: List[Dict]  # top risky functions


@dataclass
class CostModelConfig:
    hourly_rate: float
    currency: str

    # Baseline productivity assumptions (tunable)
    baseline_loc_per_hour: float  # raw implementation throughput under "normal" complexity
    overhead_multiplier: float    # meetings/reviews/context switching
    contingency_pct: float        # uncertainty buffer

    # Complexity & maintainability impact tuning
    complexity_k: float  # how much CC inflates effort
    maintainability_k: float  # how much low MI inflates effort

    # “Ideal” reference points
    ref_avg_cc: float
    ref_mi: float


@dataclass
class CostEstimate:
    effort_hours_point: float
    effort_hours_low: float
    effort_hours_high: float
    cost_point: float
    cost_low: float
    cost_high: float

    drivers: Dict[str, float]
    notes: List[str]


# ----------------------------
# Utility: file collection
# ----------------------------

DEFAULT_INCLUDE_EXT = [
    ".py", ".js", ".ts", ".java", ".kt", ".go", ".rb", ".cs", ".cpp", ".c", ".h",
    ".php", ".swift", ".rs"
]

DEFAULT_EXCLUDE_DIR = [
    ".git", ".hg", ".svn",
    "node_modules", "dist", "build", "target", "out",
    ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".idea", ".vscode"
]


def collect_files(
    root: Path,
    include_ext: List[str],
    exclude_dir: List[str],
    max_file_size_kb: int = 1024
) -> List[Path]:
    include_ext = [e.lower() for e in include_ext]
    exclude_dir_set = set(exclude_dir)

    files: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # prune excluded directories in-place
        dirnames[:] = [d for d in dirnames if d not in exclude_dir_set]

        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() not in include_ext:
                continue
            try:
                size_kb = p.stat().st_size / 1024
                if size_kb > max_file_size_kb:
                    continue
            except OSError:
                continue
            files.append(p)

    return files


def count_loc(path: Path) -> int:
    # simple LOC count (non-empty lines)
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


# ----------------------------
# Radon runners & parsers
# ----------------------------

def run_cmd(cmd: List[str], cwd: Optional[Path] = None) -> str:
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False
        )
    except FileNotFoundError:
        raise RuntimeError(f"Command not found: {cmd[0]}. Is it installed and on PATH?")

    if proc.returncode != 0 and proc.stdout.strip() == "":
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{proc.stderr}")

    return proc.stdout


def radon_cc_json(py_files: List[Path]) -> Dict:
    # radon cc -j <file1> <file2> ...
    if not py_files:
        return {}
    cmd = ["radon", "cc", "-j"] + [str(p) for p in py_files]
    out = run_cmd(cmd)
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse radon cc JSON output: {e}\nOutput:\n{out[:2000]}")


def radon_mi_json(py_files: List[Path]) -> Dict:
    # radon mi -j <files...>
    if not py_files:
        return {}
    cmd = ["radon", "mi", "-j"] + [str(p) for p in py_files]
    out = run_cmd(cmd)
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse radon mi JSON output: {e}\nOutput:\n{out[:2000]}")


def parse_radon_cc(cc_json: Dict) -> List[RadonCCFileResult]:
    results: List[RadonCCFileResult] = []
    for file, blocks in cc_json.items():
        if not blocks:
            continue
        ccs = [b.get("complexity", 0) for b in blocks if isinstance(b, dict)]
        if not ccs:
            continue
        results.append(
            RadonCCFileResult(
                file=file,
                avg_cc=float(sum(ccs) / len(ccs)),
                max_cc=int(max(ccs)),
                blocks=len(ccs),
            )
        )
    return results


def parse_radon_mi(mi_json: Dict) -> List[RadonMIFileResult]:
    results: List[RadonMIFileResult] = []
    for file, info in mi_json.items():
        # format is typically: {"mi": 67.3, "rank": "B"} per file
        if isinstance(info, dict) and "mi" in info:
            results.append(RadonMIFileResult(file=file, mi=float(info["mi"])))
    return results


# ----------------------------
# Lizard runner & parser
# ----------------------------

LIZARD_LINE_RE = re.compile(
    # Example lizard line (varies by version); we handle common patterns robustly.
    # Typical columns: NLOC CCN token param length location function
    r"^\s*(?P<nloc>\d+)\s+"
    r"(?P<cc>\d+)\s+"
    r"(?P<token>\d+)\s+"
    r"(?P<params>\d+)\s+"
    r"(?P<length>\d+)\s+"
    r"(?P<location>.+?)\s+"
    r"(?P<function>.+)$"
)

def lizard_text(files: List[Path]) -> str:
    if not files:
        return ""
    cmd = ["lizard"] + [str(p) for p in files]
    return run_cmd(cmd)


def parse_lizard(text: str) -> List[LizardFunctionResult]:
    """
    Parses lizard output. It prints a header and then function rows.
    We'll scan for rows matching the numeric columns pattern.
    """
    results: List[LizardFunctionResult] = []
    for line in text.splitlines():
        m = LIZARD_LINE_RE.match(line)
        if not m:
            continue
        nloc = int(m.group("nloc"))
        cc = int(m.group("cc"))
        params = int(m.group("params"))
        length = int(m.group("length"))
        location = m.group("location").strip()
        func = m.group("function").strip()

        # location often like "path/to/file.py:line"
        file_part = location.split(":")[0].strip()
        results.append(
            LizardFunctionResult(
                file=file_part,
                function=func,
                nloc=nloc,
                cc=cc,
                params=params,
                length=length
            )
        )
    return results


# ----------------------------
# Aggregation
# ----------------------------

def percentile(values: List[int], p: float) -> Optional[float]:
    if not values:
        return None
    values_sorted = sorted(values)
    k = (len(values_sorted) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(values_sorted[int(k)])
    d0 = values_sorted[f] * (c - k)
    d1 = values_sorted[c] * (k - f)
    return float(d0 + d1)


def build_hotspots(lizard_funcs: List[LizardFunctionResult], top_n: int = 10) -> List[Dict]:
    # Risk score: CC * log(1 + NLOC)
    scored = []
    for fn in lizard_funcs:
        score = fn.cc * math.log(1.0 + max(fn.nloc, 0))
        scored.append((score, fn))
    scored.sort(key=lambda x: x[0], reverse=True)
    hotspots = []
    for score, fn in scored[:top_n]:
        hotspots.append({
            "file": fn.file,
            "function": fn.function,
            "cc": fn.cc,
            "nloc": fn.nloc,
            "risk_score": round(score, 2),
        })
    return hotspots


def aggregate(
    all_files: List[Path],
    radon_cc: List[RadonCCFileResult],
    radon_mi: List[RadonMIFileResult],
    lizard_funcs: List[LizardFunctionResult],
) -> AggregateMetrics:
    scanned_loc = sum(count_loc(p) for p in all_files)

    py_files = [p for p in all_files if p.suffix.lower() == ".py"]

    # Radon aggregates
    radon_avg_cc = None
    radon_max_cc = None
    if radon_cc:
        radon_avg_cc = float(statistics.mean([r.avg_cc for r in radon_cc]))
        radon_max_cc = int(max(r.max_cc for r in radon_cc))

    radon_avg_mi = None
    if radon_mi:
        radon_avg_mi = float(statistics.mean([r.mi for r in radon_mi]))

    # Lizard aggregates
    lizard_ccs = [f.cc for f in lizard_funcs]
    lizard_nlocs = [f.nloc for f in lizard_funcs]
    lizard_avg_cc = float(statistics.mean(lizard_ccs)) if lizard_ccs else None
    lizard_p90_cc = percentile(lizard_ccs, 0.90) if lizard_ccs else None
    lizard_max_cc = int(max(lizard_ccs)) if lizard_ccs else None
    lizard_avg_nloc = float(statistics.mean(lizard_nlocs)) if lizard_nlocs else None

    return AggregateMetrics(
        scanned_files=len(all_files),
        scanned_loc=int(scanned_loc),

        python_files=len(py_files),
        radon_cc_files=len(radon_cc),
        radon_mi_files=len(radon_mi),
        radon_avg_cc=radon_avg_cc,
        radon_max_cc=radon_max_cc,
        radon_avg_mi=radon_avg_mi,

        lizard_files=len(set(f.file for f in lizard_funcs)),
        lizard_functions=len(lizard_funcs),
        lizard_avg_cc=lizard_avg_cc,
        lizard_p90_cc=lizard_p90_cc,
        lizard_max_cc=lizard_max_cc,
        lizard_avg_nloc=lizard_avg_nloc,
        hotspots=build_hotspots(lizard_funcs, top_n=10),
    )


# ----------------------------
# Cost model
# ----------------------------

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def estimate_cost(metrics: AggregateMetrics, cfg: CostModelConfig) -> CostEstimate:
    """
    Heuristic model:
      base_hours = LOC / baseline_loc_per_hour
      apply overhead multiplier
      complexity_factor = 1 + complexity_k * max(0, (avg_cc - ref_avg_cc)/ref_avg_cc)
      maintainability_factor = 1 + maintainability_k * max(0, (ref_mi - avg_mi)/ref_mi)
    Then apply contingency and produce low/high range.
    """

    loc = max(metrics.scanned_loc, 0)

    # Base throughput: if LOC=0, cost is 0
    base_hours = (loc / cfg.baseline_loc_per_hour) if loc > 0 else 0.0

    # choose complexity & MI inputs:
    # Prefer lizard for CC (multi-language); fall back to radon if lizard is empty.
    avg_cc = metrics.lizard_avg_cc if metrics.lizard_avg_cc is not None else metrics.radon_avg_cc
    if avg_cc is None:
        avg_cc = cfg.ref_avg_cc  # neutral

    # Prefer radon MI (Python-only). If absent, neutral.
    avg_mi = metrics.radon_avg_mi if metrics.radon_avg_mi is not None else cfg.ref_mi

    # Factors
    cc_inflation = max(0.0, (avg_cc - cfg.ref_avg_cc) / cfg.ref_avg_cc)
    complexity_factor = 1.0 + cfg.complexity_k * cc_inflation
    complexity_factor = clamp(complexity_factor, 0.8, 3.0)

    mi_deficit = max(0.0, (cfg.ref_mi - avg_mi) / cfg.ref_mi)
    maintainability_factor = 1.0 + cfg.maintainability_k * mi_deficit
    maintainability_factor = clamp(maintainability_factor, 0.85, 2.5)

    overhead_factor = cfg.overhead_multiplier

    point_hours = base_hours * overhead_factor * complexity_factor * maintainability_factor

    # Add contingency (uncertainty buffer)
    point_hours_with_cont = point_hours * (1.0 + cfg.contingency_pct / 100.0)

    # Build low/high: ±20% plus sensitivity to hotspots (p90 complexity)
    # If p90 CC is high, widen range a bit.
    p90 = metrics.lizard_p90_cc if metrics.lizard_p90_cc is not None else avg_cc
    widen = 0.20
    if p90 is not None and p90 >= 20:
        widen = 0.30
    elif p90 is not None and p90 >= 30:
        widen = 0.40

    low_hours = point_hours_with_cont * (1.0 - widen)
    high_hours = point_hours_with_cont * (1.0 + widen)

    # Costs
    point_cost = point_hours_with_cont * cfg.hourly_rate
    low_cost = low_hours * cfg.hourly_rate
    high_cost = high_hours * cfg.hourly_rate

    drivers = {
        "base_hours_from_loc": round(base_hours, 2),
        "overhead_multiplier": round(overhead_factor, 3),
        "avg_cc_used": round(float(avg_cc), 3),
        "complexity_factor": round(complexity_factor, 3),
        "avg_mi_used": round(float(avg_mi), 3),
        "maintainability_factor": round(maintainability_factor, 3),
        "contingency_pct": cfg.contingency_pct,
        "range_widen_pct": int(widen * 100),
    }

    notes = [
        "This is a heuristic estimate from code metrics (LOC/CC/MI). It does not measure true human effort.",
        "Radon MI applies only to Python files; for mixed-language repos, MI may be neutral.",
        "Cost range is widened when high-complexity hotspots (high CC) are detected.",
    ]

    return CostEstimate(
        effort_hours_point=round(point_hours_with_cont, 2),
        effort_hours_low=round(low_hours, 2),
        effort_hours_high=round(high_hours, 2),
        cost_point=round(point_cost, 2),
        cost_low=round(low_cost, 2),
        cost_high=round(high_cost, 2),
        drivers=drivers,
        notes=notes,
    )


# ----------------------------
# Reporting
# ----------------------------

def fmt_money(value: float, currency: str) -> str:
    # simple currency formatting (no locale)
    return f"{currency} {value:,.2f}"


def print_report(metrics: AggregateMetrics, est: CostEstimate, cfg: CostModelConfig) -> None:
    print("\n==============================")
    print("EXECUTIVE SUMMARY (ESTIMATED)")
    print("==============================")
    print(f"Scanned files:           {metrics.scanned_files}")
    print(f"Scanned LOC (non-empty): {metrics.scanned_loc:,}")
    print("")
    print("Complexity & Maintainability (proxies):")
    print(f"  Lizard functions:      {metrics.lizard_functions}")
    print(f"  Lizard avg CC:         {metrics.lizard_avg_cc if metrics.lizard_avg_cc is not None else 'n/a'}")
    print(f"  Lizard p90 CC:         {metrics.lizard_p90_cc if metrics.lizard_p90_cc is not None else 'n/a'}")
    print(f"  Lizard max CC:         {metrics.lizard_max_cc if metrics.lizard_max_cc is not None else 'n/a'}")
    print(f"  Python files:          {metrics.python_files}")
    print(f"  Radon avg MI (Python): {metrics.radon_avg_mi if metrics.radon_avg_mi is not None else 'n/a'}")
    print(f"  Radon avg CC (Python): {metrics.radon_avg_cc if metrics.radon_avg_cc is not None else 'n/a'}")
    print("")

    print("Effort & Cost Estimate:")
    print(f"  Hourly rate:           {fmt_money(cfg.hourly_rate, cfg.currency)} / hour")
    print(f"  Point estimate hours:  {est.effort_hours_point:,.2f} h")
    print(f"  Range (low–high):      {est.effort_hours_low:,.2f} h  –  {est.effort_hours_high:,.2f} h")
    print(f"  Point estimate cost:   {fmt_money(est.cost_point, cfg.currency)}")
    print(f"  Range (low–high):      {fmt_money(est.cost_low, cfg.currency)}  –  {fmt_money(est.cost_high, cfg.currency)}")
    print("")

    print("Top Hotspots (highest risk score = high complexity × size):")
    if metrics.hotspots:
        for i, h in enumerate(metrics.hotspots, 1):
            print(f"  {i:>2}. {h['file']} :: {h['function']} | CC={h['cc']} NLOC={h['nloc']} score={h['risk_score']}")
    else:
        print("  (none detected)")

    print("\nModel Drivers (transparency):")
    for k, v in est.drivers.items():
        print(f"  - {k}: {v}")

    print("\nNotes:")
    for n in est.notes:
        print(f"  - {n}")
    print("")


def build_json(metrics: AggregateMetrics, est: CostEstimate, cfg: CostModelConfig) -> Dict:
    return {
        "metrics": asdict(metrics),
        "cost_model_config": asdict(cfg),
        "estimate": asdict(est),
    }


# ----------------------------
# Main
# ----------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Effort/Cost estimation from code scanning (radon + lizard).")
    ap.add_argument("path", help="Repository or codebase root path")
    ap.add_argument("--include-ext", nargs="*", default=DEFAULT_INCLUDE_EXT, help="File extensions to include")
    ap.add_argument("--exclude-dir", nargs="*", default=DEFAULT_EXCLUDE_DIR, help="Directory names to exclude")
    ap.add_argument("--max-file-size-kb", type=int, default=1024, help="Skip files larger than this (KB)")

    ap.add_argument("--hourly-rate", type=float, default=50.0, help="Hourly rate used for cost estimation")
    ap.add_argument("--currency", type=str, default="USD", help="Currency label (e.g., USD, INR, EUR)")

    ap.add_argument("--baseline-loc-per-hour", type=float, default=20.0,
                    help="Baseline throughput: non-empty LOC per hour (before complexity/MI/overhead)")
    ap.add_argument("--overhead-multiplier", type=float, default=1.6,
                    help="Overhead for reviews/meetings/context switching (e.g., 1.6 = +60%)")
    ap.add_argument("--contingency-pct", type=float, default=15.0,
                    help="Uncertainty buffer percent added to point estimate")

    ap.add_argument("--complexity-k", type=float, default=0.9,
                    help="How strongly complexity inflates effort (0.0–1.5 typical)")
    ap.add_argument("--maintainability-k", type=float, default=0.7,
                    help="How strongly low maintainability inflates effort (0.0–1.5 typical)")

    ap.add_argument("--ref-avg-cc", type=float, default=10.0,
                    help="Reference average cyclomatic complexity (neutral point)")
    ap.add_argument("--ref-mi", type=float, default=75.0,
                    help="Reference maintainability index (neutral point)")

    ap.add_argument("--json-out", type=str, default=None, help="Write full report JSON to this file")
    args = ap.parse_args()

    root = Path(args.path).resolve()
    if not root.exists():
        print(f"ERROR: path does not exist: {root}", file=sys.stderr)
        return 2

    files = collect_files(
        root=root,
        include_ext=args.include_ext,
        exclude_dir=args.exclude_dir,
        max_file_size_kb=args.max_file_size_kb,
    )

    if not files:
        print("No files found with the selected extensions. Adjust --include-ext / --exclude-dir.")
        return 1

    py_files = [p for p in files if p.suffix.lower() == ".py"]

    # Run analyses
    radon_cc_res: List[RadonCCFileResult] = []
    radon_mi_res: List[RadonMIFileResult] = []
    lizard_funcs: List[LizardFunctionResult] = []

    # Radon (Python only)
    try:
        ccj = radon_cc_json(py_files)
        mij = radon_mi_json(py_files)
        radon_cc_res = parse_radon_cc(ccj)
        radon_mi_res = parse_radon_mi(mij)
    except RuntimeError as e:
        print(f"WARNING: Radon analysis failed or partially failed: {e}", file=sys.stderr)

    # Lizard (multi-language)
    try:
        liz_text = lizard_text(files)
        lizard_funcs = parse_lizard(liz_text)
    except RuntimeError as e:
        print(f"ERROR: Lizard analysis failed: {e}", file=sys.stderr)
        return 3

    metrics = aggregate(files, radon_cc_res, radon_mi_res, lizard_funcs)

    cfg = CostModelConfig(
        hourly_rate=float(args.hourly_rate),
        currency=str(args.currency),

        baseline_loc_per_hour=float(args.baseline_loc_per_hour),
        overhead_multiplier=float(args.overhead_multiplier),
        contingency_pct=float(args.contingency_pct),

        complexity_k=float(args.complexity_k),
        maintainability_k=float(args.maintainability_k),

        ref_avg_cc=float(args.ref_avg_cc),
        ref_mi=float(args.ref_mi),
    )

    est = estimate_cost(metrics, cfg)

    print_report(metrics, est, cfg)

    if args.json_out:
        out_path = Path(args.json_out).resolve()
        out_path.write_text(json.dumps(build_json(metrics, est, cfg), indent=2), encoding="utf-8")
        print(f"Wrote JSON report to: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
