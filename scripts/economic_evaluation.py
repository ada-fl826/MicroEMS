"""Replay the existing held-out scenario; run with python -m scripts.economic_evaluation."""

import hashlib
import json
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

from backend.data import (
    DT,
    load_source_data,
    prepare_scenario,
    rolling_forecast,
    scenario_catalog,
)
from backend.optimizer import solve_dispatch
from backend.schemas import CreateSimulation, EnterpriseSettings

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "results" / "economic_evaluation"
NAMES = ["No Battery", "Self-Consumption RBC", "Time-of-Use RBC", "Intelligent EMS"]
COLORS = ["#7C8389", "#8EACC1", "#C49D76", "#476C85"]
TOL = 1e-5


def validate_source_windows():
    """Reject gaps in the seven-day history and the actual evaluation day."""
    pv, load, _ = load_source_data()
    catalog = scenario_catalog()
    for frame, column, day in (
        (pv, "sampling_end_time", catalog["pv_day_index"]),
        (load, "times", catalog["load_day_index"]),
    ):
        times = pd.to_datetime(frame[column].iloc[(day - 7) * 96 : (day + 1) * 96])
        if (
            len(times) != 8 * 96
            or not times.diff().iloc[1:].eq(pd.Timedelta(minutes=15)).all()
        ):
            raise ValueError(
                "Replay history and observations must be continuous at 15 minutes."
            )


def available_forecast(scenario, step):
    """At a slot's start only previous slots' actuals may correct the forecast."""
    if step == 0:
        return scenario["forecast"]
    return rolling_forecast(
        scenario["forecast"],
        step - 1,
        scenario["actual_pv_kw"][step - 1],
        scenario["actual_load_kw"][step - 1],
        scenario["pv_capacity_kw"],
    )[1:]


def feasible_action(desired, energy, net, remaining, request):
    """Apply common actuator limits and a reachable equal-energy endpoint.

    Positive power charges. Only currently measured net power is used by this
    safety layer. Grid export overflow is handled by PV curtailment afterwards.
    """
    b, e = request.equipment, request.enterprise
    target = b.terminal_soc * b.capacity_kwh
    minimum = max(
        e.reserve_soc * b.capacity_kwh,
        target - remaining * b.charge_limit_kw * b.efficiency * DT,
    )
    maximum = min(
        b.max_soc * b.capacity_kwh,
        target + remaining * b.discharge_limit_kw / b.efficiency * DT,
    )

    def power(delta):
        return delta / (b.efficiency * DT) if delta >= 0 else delta * b.efficiency / DT

    lower = max(-b.discharge_limit_kw, power(minimum - energy))
    import_limit = b.grid_import_capacity_kw
    if e.import_hard_limit_kw is not None:
        import_limit = min(import_limit, e.import_hard_limit_kw)
    upper = min(b.charge_limit_kw, power(maximum - energy), import_limit - net)
    if lower > upper + TOL:
        raise ValueError("Actual conditions cannot satisfy grid/SOC/terminal limits.")
    return float(np.clip(desired, lower, max(lower, upper)))


def replay(scenario, name, request):
    b = request.equipment
    energy = b.initial_soc * b.capacity_kwh
    pv_energy = 0.0  # Initial battery inventory has no claimed PV origin.
    records = []
    prices = [p["buy_price"] for p in scenario["forecast"]]
    for t, point in enumerate(scenario["forecast"]):
        pv, load = scenario["actual_pv_kw"][t], scenario["actual_load_kw"][t]
        net = load - pv
        if name == "No Battery":
            desired = action = 0.0
        else:
            if name == "Intelligent EMS":
                plan = solve_dispatch(
                    available_forecast(scenario, t), request, energy / b.capacity_kwh
                )
                first = plan["points"][0]
                desired = first["charge_kw"] - first["discharge_kw"]
            elif name == "Time-of-Use RBC" and point["buy_price"] == min(prices):
                desired = b.charge_limit_kw
            else:
                # SC follows net power; TOU discharges only at the highest price.
                desired = -net
                if (
                    name == "Time-of-Use RBC"
                    and net > 0
                    and point["buy_price"] != max(prices)
                ):
                    desired = 0.0
            action = feasible_action(desired, energy, net, len(prices) - t - 1, request)
        charge, discharge = max(action, 0.0), max(-action, 0.0)
        pv_fraction = pv_energy / energy if energy > TOL else 0.0
        pv_to_load = min(pv, load) + min(discharge, max(net, 0)) * pv_fraction
        pv_charge = min(charge, max(-net, 0))
        pv_energy += DT * (
            pv_charge * b.efficiency - discharge * pv_fraction / b.efficiency
        )
        previous_energy = energy
        energy += DT * (charge * b.efficiency - discharge / b.efficiency)
        grid = net + action
        imp = max(grid, 0.0)
        exp = min(max(-grid, 0.0), b.grid_export_capacity_kw)
        curtail = max(-grid - exp, 0.0)
        limit = b.grid_import_capacity_kw
        if request.enterprise.import_hard_limit_kw is not None:
            limit = min(limit, request.enterprise.import_hard_limit_kw)
        violations = {
            "soc": name != "No Battery"
            and not (
                request.enterprise.reserve_soc * b.capacity_kwh - TOL
                <= energy
                <= b.max_soc * b.capacity_kwh + TOL
            ),
            "power": charge > b.charge_limit_kw + TOL
            or discharge > b.discharge_limit_kw + TOL,
            "grid": imp > limit + TOL or exp > b.grid_export_capacity_kw + TOL,
            "balance": abs(pv + imp + discharge - load - charge - exp - curtail) > TOL,
            "simultaneous": min(charge, discharge) > TOL or min(imp, exp) > TOL,
            "energy": abs(
                energy
                - previous_energy
                - DT * (charge * b.efficiency - discharge / b.efficiency)
            )
            > TOL,
            "curtailment": curtail > pv + TOL,
            "terminal": name != "No Battery"
            and t == len(prices) - 1
            and abs(energy - b.terminal_soc * b.capacity_kwh) > TOL,
        }
        records.append(
            {
                "scenario": name,
                "slot": t,
                "time": point["time"],
                "forecast_issued_after_slot": t - 1
                if name == "Intelligent EMS"
                else None,
                "pv_kw": pv,
                "load_kw": load,
                "buy_price": point["buy_price"],
                "sell_price": point["sell_price"],
                "desired_battery_kw": desired,
                "charge_kw": charge,
                "discharge_kw": discharge,
                "import_kw": imp,
                "export_kw": exp,
                "curtail_kw": curtail,
                "soc": energy / b.capacity_kwh if name != "No Battery" else np.nan,
                "pv_delivered_kwh": pv_to_load * DT,
                "pv_charge_kwh": pv_charge * DT,
                "pv_inventory_kwh": max(0, pv_energy),
                "cost_yuan": DT
                * (imp * point["buy_price"] - exp * point["sell_price"]),
                "throughput_cost_yuan": 0.005 * DT * (charge + discharge),
                "action_adjusted": abs(action - desired) > TOL,
                **{f"violation_{key}": int(value) for key, value in violations.items()},
            }
        )
    frame = pd.DataFrame(records)
    frame["cumulative_cost_yuan"] = frame.cost_yuan.cumsum()
    return frame


def summarize(dispatch):
    rows = []
    for name in NAMES:
        f = dispatch[dispatch.scenario == name]
        rows.append(
            {
                "scenario": name,
                "total_cost_yuan": f.cost_yuan.sum(),
                "peak_import_kw": f.import_kw.max(),
                "pv_self_consumption_pct": 100
                * f.pv_delivered_kwh.sum()
                / (f.pv_kw.sum() * DT),
                "import_kwh": f.import_kw.sum() * DT,
                "export_kwh": f.export_kw.sum() * DT,
                "curtail_kwh": f.curtail_kw.sum() * DT,
                "throughput_proxy_yuan": f.throughput_cost_yuan.sum(),
                "final_soc": f.soc.iloc[-1],
                "final_pv_inventory_kwh": f.pv_inventory_kwh.iloc[-1],
                "adjusted_steps": int(f.action_adjusted.sum()),
                "constraint_violations": int(
                    f.filter(like="violation_").to_numpy().sum()
                ),
            }
        )
    result = pd.DataFrame(rows)
    baseline = result.total_cost_yuan.iloc[0]
    result["reduction_vs_no_battery_pct"] = (
        100 * (baseline - result.total_cost_yuan) / baseline
    )
    for index, key in ((1, "sc_rbc"), (2, "tou_rbc")):
        reference = result.total_cost_yuan.iloc[index]
        result[f"savings_vs_{key}_pct"] = (
            100 * (reference - result.total_cost_yuan) / reference
        )
    return result


def plot_results(dispatch, comparison, output):
    """Three quantitative comparisons, with identical method colors and source CSVs."""
    plt.rcParams.update(
        {
            "font.size": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "svg.fonttype": "none",
        }
    )

    def save(fig, name):
        for extension in ("png", "svg"):
            fig.savefig(output / f"{name}.{extension}", dpi=180)
        plt.close(fig)

    fig, ax = plt.subplots(figsize=(9, 4.4), layout="constrained")
    labels = [
        "No Battery",
        "Self-Consumption\nRBC",
        "Time-of-Use\nRBC",
        "Intelligent EMS",
    ]
    bars = ax.bar(labels, comparison.total_cost_yuan, color=COLORS)
    ax.bar_label(bars, fmt="%.3f", padding=4)
    ax.set(
        ylabel="Electricity cost (yuan / 24 h)",
        title="Historical scenario: actual electricity settlement",
    )
    ax.margins(y=0.18)
    save(fig, "cost_comparison")

    fig, ax = plt.subplots(figsize=(9, 4.4), layout="constrained")
    for name, color in zip(NAMES, COLORS):
        f = dispatch[dispatch.scenario == name]
        ax.plot(
            np.arange(len(f) + 1) * DT,
            np.r_[0, f.cumulative_cost_yuan],
            label=name,
            color=color,
        )
    ax.set(
        xlabel="Replay hour", ylabel="Cumulative electricity cost (yuan)", xlim=(0, 24)
    )
    ax.legend(loc="upper left", ncol=2, fontsize=9)
    save(fig, "cumulative_cost")

    fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True, layout="constrained")
    f = dispatch[dispatch.scenario == NAMES[0]]
    hours = np.arange(len(f)) * DT
    axes[0].step(hours, f.pv_kw, where="post", label="Actual PV", color="#C49D76")
    axes[0].step(hours, f.load_kw, where="post", label="Actual load", color="#7C8389")
    axes[0].set_ylabel("Power (kW)")
    axes[0].legend(ncol=2)
    for name, color in zip(NAMES[1:], COLORS[1:]):
        f = dispatch[dispatch.scenario == name]
        axes[1].step(
            hours, f.charge_kw - f.discharge_kw, where="post", label=name, color=color
        )
        axes[2].plot(np.arange(len(f) + 1) * DT, np.r_[50, f.soc * 100], color=color)
    axes[1].axhline(0, color="#AAAAAA", linewidth=0.6)
    axes[1].set_ylabel("Battery (kW)\n+ charge / - discharge")
    axes[1].legend(ncol=3, fontsize=9)
    axes[2].axhspan(30, 90, alpha=0.08, color="#476C85")
    axes[2].set(xlabel="Replay hour", ylabel="SOC (%)", xlim=(0, 24), ylim=(25, 95))
    save(fig, "dispatch_soc")


def write_report(scenario, comparison):
    a, b, c, d = comparison.to_dict("records")
    rows = []
    for r in (a, b, c, d):
        rows.append(
            f"| {r['scenario']} | {r['total_cost_yuan']:.4f} | "
            f"{r['reduction_vs_no_battery_pct']:.2f} | {r['savings_vs_sc_rbc_pct']:.2f} | "
            f"{r['savings_vs_tou_rbc_pct']:.2f} | {r['peak_import_kw']:.3f} | "
            f"{r['pv_self_consumption_pct']:.2f} |"
        )
    source = scenario["source_info"]
    report = f"""# EMS Economic Impact Evaluation

## Setup

- **Scope:** one 24-hour replay with 96 fifteen-minute intervals. PV: {source["pv_source"]}. Load: {source["load_source"]}. Data from different regions and years are aligned by interval. This is a scaled offline scenario, not a measured site bill or an annual savings estimate.
- **Data preparation:** reuse `backend.data.prepare_scenario`. Scale PV and load separately to 3 kW using only the preceding seven-day peaks; cap PV at 3 kW. No observations are synthesized, and no model is retrained or fitted to the evaluation day. The PV day follows all 500 days used by the training script. Load uses the last complete test day.
- **Source coverage:** PV spans 2007-01-02 00:00 to 2020-10-06 22:45 UTC (482,588 rows). Load spans 2016-01-01 to 2016-03-31 (8,736 rows). Both use fifteen-minute samples. The replay day and preceding seven days pass continuity checks. Existing artifacts contain weights and prediction plots, but no reusable forecast table with issue times, so forecasts are recomputed by inference.
- **Equipment:** a 5 kWh battery, 2 kW charge/discharge limits, 95% efficiency in each direction (90.25% round trip), and 30–90% SOC. Initial and final SOC both equal 50%. All scenarios use 5 kW import and 3 kW export limits. Excess PV is curtailed. Simultaneous charging/discharging and importing/exporting are prohibited.
- **Tariff:** reuse CSV prices in yuan/kWh. Imports cost 0.288 during 00:00–08:00 and 22:00–24:00, and 0.568 otherwise. Exports earn 0.4153. Bills include energy only; demand charges, fixed fees, and taxes are excluded because supporting tariff data are unavailable.
- **Export assumption:** stored energy may be exported under the existing model. Off-peak imports are cheaper than exports, allowing grid-to-battery-to-grid arbitrage. Contract eligibility has not been verified; savings are conditional on this assumption.
- **Rule-based control (RBC):** self-consumption RBC charges from PV surplus and discharges into deficits. Time-of-use (TOU) RBC charges at the lowest import price and discharges into deficits at the highest price. Other intervals prioritize direct PV use. Thresholds come from the tariff, without tuning on replay results.
- **Intelligent EMS:** reuse pretrained PV LSTM-Transformer and load LSTM models with the existing Gurobi MILP optimizer. Reoptimize the remaining day at each interval and execute only the first action. Use the cost objective, disable the default 1.5 kW soft import target, and retain existing physical constraints.
- **Causality:** slot 0 uses day-ahead forecasts. At slot t, residual correction uses only observations through slot t−1. Current and future actuals never enter the optimizer. Current net load is used only for common actuator limits and settlement. The replay uses interval-average power and does not simulate faster device dynamics.
- **Equal endpoints:** a common reachable-energy envelope returns every battery scenario to its initial 50% SOC. Recovery imports and exports are billed. This envelope can override RBC actions; no separate terminal-energy credit is added.
- **Accounting:** bill = sum of 0.25 × (actual imports × import price − actual exports × export price). Battery aging is excluded. The optimizer retains its 0.005 yuan/kWh throughput penalty as a regularizer, reported separately rather than treated as measured degradation cost.

## Comparison

Negative savings indicate a higher bill than the reference. Each percentage uses its reference bill as the denominator.

| Scenario | Electricity Cost (yuan) | Reduction vs No Battery (%) | Savings vs SC RBC (%) | Savings vs TOU RBC (%) | Peak Import (kW) | PV Self-Consumption (%) |
|---|---:|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

PV self-consumption is **PV energy delivered to load / total PV generation**. PV serves load first; surplus can charge the battery. Stored PV is tracked proportionally, and initial inventory has no PV origin. Losses, battery exports, and unused terminal PV inventory are excluded. This avoids counting grid charging as PV use and differs from 1 − total exports / PV generation.

| Scenario | Import (kWh) | Export (kWh) | Throughput Proxy (yuan) | Final SOC | Final PV Inventory (kWh) | Adjusted Slots | Violations |
|---|---:|---:|---:|---:|---:|---:|---:|
"""
    for r in (a, b, c, d):
        soc = "—" if r["scenario"] == NAMES[0] else f"{r['final_soc']:.2%}"
        report += (
            f"| {r['scenario']} | {r['import_kwh']:.3f} | {r['export_kwh']:.3f} | "
            f"{r['throughput_proxy_yuan']:.4f} | {soc} | {r['final_pv_inventory_kwh']:.4f} | "
            f"{r['adjusted_steps']} | {r['constraint_violations']} |\n"
        )
    report += f"""
Adjusted slots reflect actuator, import, or endpoint corrections, not constraint violations. Checks cover SOC, power, grid capacity, balance, energy recursion, mutually exclusive actions, curtailment, and terminal energy (tolerance 1e−5). Per-slot checks are in `dispatch.csv`.

## Figures

![Electricity cost comparison](results/economic_evaluation/cost_comparison.png)

Bills use identical actuals and settlement periods. This single-day comparison has no statistical error bars.

![Cumulative electricity cost](results/economic_evaluation/cumulative_cost.png)

All curves start at zero and include endpoint recovery costs.

![Battery dispatch and SOC](results/economic_evaluation/dispatch_soc.png)

Top: actual PV and load. Middle: battery power, positive for charging and negative for discharging. Bottom: continuous SOC; shading marks the allowed range.

## Findings and limits

- Battery plus EMS saved **{a["total_cost_yuan"] - d["total_cost_yuan"]:.4f} yuan over 24 hours ({d["reduction_vs_no_battery_pct"]:.2f}%)** versus no battery.
- EMS saved **{b["total_cost_yuan"] - d["total_cost_yuan"]:.4f} yuan ({d["savings_vs_sc_rbc_pct"]:.2f}%)** versus self-consumption RBC. Its bill differed from TOU RBC by **{d["total_cost_yuan"] - c["total_cost_yuan"]:+.6f} yuan ({100 * (d["total_cost_yuan"] / c["total_cost_yuan"] - 1):+.3f}%)**. Positive differences mean EMS cost more. This sub-cent difference does not establish a robust ranking.
- Peak import changed from {a["peak_import_kw"]:.3f} to {d["peak_import_kw"]:.3f} kW (**{100 * (d["peak_import_kw"] / a["peak_import_kw"] - 1):+.2f}%**). PV self-consumption changed from {a["pv_self_consumption_pct"]:.2f}% to {d["pv_self_consumption_pct"]:.2f}% (**{d["pv_self_consumption_pct"] - a["pv_self_consumption_pct"]:+.2f} percentage points**). Minimizing energy cost does not guarantee peak reduction; cheap charging and endpoint recovery can raise peaks.
- Including the throughput proxy gives {c["total_cost_yuan"] + c["throughput_proxy_yuan"]:.4f} yuan for TOU RBC and {d["total_cost_yuan"] + d["throughput_proxy_yuan"]:.4f} yuan for EMS. The proxy can change the objective ranking, but it is not measured aging cost or proof of the sole cause of the bill difference.
- No battery to self-consumption RBC saved {a["total_cost_yuan"] - b["total_cost_yuan"]:.4f} yuan, combining battery availability with that rule. Comparisons with EMS measure the combined effect of forecasting and rolling optimization. Separate ablations are needed to isolate either component. Results also depend on tariff and battery-export assumptions.
- One existing test day cannot establish seasonal reliability, annual returns, or real-contract savings. Capital, maintenance, and measured aging costs are excluded.

**CV summary:** Built a causal 15-minute replay benchmark using pretrained LSTM forecasts and Gurobi rolling optimization. Reduced electricity cost by {d["reduction_vs_no_battery_pct"]:.2f}% versus no battery and {d["savings_vs_sc_rbc_pct"]:.2f}% versus self-consumption RBC, with {d["constraint_violations"]} constraint violations. Results cover one scaled offline day with equal battery endpoints; the EMS–TOU bill difference was {100 * (d["total_cost_yuan"] / c["total_cost_yuan"] - 1):+.3f}%.

## Reproduction and audit

Run `python -m scripts.economic_evaluation` from the project root with the project environment activated. Existing model weights, CSV inputs, Python dependencies, and a working Gurobi license are required. No training is performed. The script writes this report and `results/economic_evaluation/` without changing application services.

- `comparison.csv`: scenario metrics.
- `dispatch.csv`: per-slot settlement and constraint checks for all four scenarios.
- `forecast_audit.csv`: forecasts and issue slots for every decision.
- `metadata.json`: source coverage, settings, and SHA-256 hashes of inputs, models, and evaluation code.
- PNG figures for viewing and editable SVG versions.
"""
    (ROOT / "economic_evaluation.md").write_text(report, encoding="utf-8")


def main():
    validate_source_windows()
    scenario = prepare_scenario()
    request = CreateSimulation(enterprise=EnterpriseSettings(import_target_kw=None))
    frames = []
    for name in NAMES:
        print(f"Replaying {name} ...", flush=True)
        frames.append(replay(scenario, name, request))
    dispatch = pd.concat(frames, ignore_index=True)
    comparison = summarize(dispatch)
    if comparison.constraint_violations.sum():
        raise ValueError(
            "Constraint violations detected; no successful report generated."
        )
    OUTPUT.mkdir(parents=True, exist_ok=True)
    dispatch.to_csv(OUTPUT / "dispatch.csv", index=False)
    comparison.to_csv(OUTPUT / "comparison.csv", index=False)
    audit = [
        {"decision_slot": t, "issued_after_slot": t - 1, **point}
        for t in range(len(scenario["forecast"]))
        for point in available_forecast(scenario, t)
    ]
    pd.DataFrame(audit).to_csv(OUTPUT / "forecast_audit.csv", index=False)
    files = [
        "data/gansu_pv.csv",
        "data/quanzhou_load.csv",
        "data/electricity_prices.csv",
        "outputs/pv/best_model.pt",
        "outputs/load/best_model.pt",
        "backend/optimizer.py",
        "backend/data.py",
        "forecasting/predict.py",
        "scripts/economic_evaluation.py",
    ]
    pv, load, _ = load_source_data()
    metadata = {
        "source_info": scenario["source_info"],
        "request": request.model_dump(),
        "replay_hours": 24,
        "interval_hours": DT,
        "pv_source_range": [str(pv.iloc[0, 0]), str(pv.iloc[-1, 0])],
        "load_source_range": [str(load.times.iloc[0]), str(load.times.iloc[-1])],
        "source_rows": {"pv": len(pv), "load": len(load)},
        "sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in files
        },
        "endpoint": "exact initial SOC; common reachable-energy actuator envelope",
        "pv_self_consumption": "PV delivered to load / actual PV generation; proportional battery provenance",
    }
    (OUTPUT / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    plot_results(dispatch, comparison, OUTPUT)
    write_report(scenario, comparison)
    print(comparison.to_string(index=False))


if __name__ == "__main__":
    main()
