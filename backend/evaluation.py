"""Score saved forecasts against executed observations, never corrected current values."""

from math import sqrt


def error_metrics(records, predicted_key, actual_key):
    errors = [
        point[predicted_key] - point[actual_key]
        for point in records
        if point[predicted_key] is not None
    ]
    count = len(errors)
    return {
        "sample_count": count,
        "mae_kw": sum(abs(error) for error in errors) / count if count else None,
        "rmse_kw": sqrt(sum(error**2 for error in errors) / count) if count else None,
    }


def forecast_accuracy(state):
    """Score the forecast issued before each observed slot."""
    ahead = {point["slot"]: point for point in state["forecast"]}
    revisions = {revision["step"]: revision for revision in state["revisions"]}
    records = []
    for actual in state["executions"]:
        slot = actual["slot"]
        predicted = ahead[slot]
        rolling = predicted if slot == 0 else None
        revision = revisions.get(slot)
        if revision is None or revision.get("issued_after_slot") != slot - 1:
            # Legacy revisions included their own slot's observation.
            revision = revisions.get(slot - 1)
        if revision is not None:
            rolling = next(
                (p for p in revision["plan"]["points"] if p["slot"] == slot), rolling
            )
        records.append(
            {
                "slot": slot,
                "time": actual["time"],
                "day_ahead_pv_kw": predicted["pv_kw"],
                "day_ahead_load_kw": predicted["load_kw"],
                "rolling_pv_kw": rolling["pv_kw"] if rolling is not None else None,
                "rolling_load_kw": rolling["load_kw"] if rolling is not None else None,
                "rolling_issued_after_slot": slot - 1 if rolling is not None else None,
                "actual_pv_kw": actual["pv_kw"],
                "actual_load_kw": actual["load_kw"],
            }
        )
    metrics = {
        f"{method}_{source}": error_metrics(
            records, f"{method}_{source}_kw", f"actual_{source}_kw"
        )
        for method in ("day_ahead", "rolling")
        for source in ("pv", "load")
    }
    return records, metrics
