"""Settle a fixed battery action against interval-average observations."""

from backend.data import DT


def settle_interval(point, pv, load, charge, discharge, request):
    net = load - pv + charge - discharge
    imported = max(0.0, net)
    surplus = max(0.0, -net)
    # Only PV can be curtailed. Any remaining export still counts against the limit.
    curtailed = min(pv, max(0.0, surplus - request.equipment.grid_export_capacity_kw))
    exported = surplus - curtailed
    target = request.enterprise.import_target_kw
    return {
        "pv_kw": pv,
        "load_kw": load,
        "import_kw": imported,
        "export_kw": exported,
        "curtail_kw": curtailed,
        "cost_yuan": DT
        * (imported * point["buy_price"] - exported * point["sell_price"]),
        "target_excess_kw": max(0.0, imported - target) if target is not None else 0.0,
    }
