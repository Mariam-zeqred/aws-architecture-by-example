import json

import gate


def metrics(**over) -> dict:
    base = {"rows": 100, "holdout_rows": 50, "mae": 1.0, "baseline_mae": 1.5, "champion_mae": None}
    base.update(over)
    return base


def test_decide_rules():
    assert gate.decide(metrics())[0] is True  # no champion, beats baseline
    assert gate.decide(metrics(mae=1.6))[0] is False  # worse than yesterday
    assert gate.decide(metrics(champion_mae=1.0))[0] is True  # equal is fine
    assert gate.decide(metrics(champion_mae=0.9))[0] is False  # worse by a little is worse
    assert gate.decide(metrics(mae=0.8, champion_mae=0.9))[0] is True


def test_gate_promotes_by_copying_and_writes_a_pointer(tmp_path):
    store = gate.LocalStore(str(tmp_path))
    store.put("models/candidate/2026-10-19/model.joblib", b"model-bytes")
    store.put("models/candidate/2026-10-19/metrics.json", json.dumps(metrics()).encode())
    result = gate.lambda_handler({"run_id": "2026-10-19"}, store=store)
    assert result["promoted"] and result["model_available"]
    assert store.get("models/current/model.joblib") == b"model-bytes"
    assert json.loads(store.get("models/current/pointer.json"))["run_id"] == "2026-10-19"

    # The next night's candidate is worse: nothing moves, the model is still available.
    store.put(
        "models/candidate/2026-10-20/metrics.json",
        json.dumps(metrics(mae=1.2, champion_mae=1.0)).encode(),
    )
    result = gate.lambda_handler({"run_id": "2026-10-20"}, store=store)
    assert not result["promoted"] and result["model_available"]
    assert "worse than the current model" in result["reason"]
    assert json.loads(store.get("models/current/pointer.json"))["run_id"] == "2026-10-19"


def test_gate_reports_no_model_when_nothing_was_ever_promoted(tmp_path):
    store = gate.LocalStore(str(tmp_path))
    store.put("models/candidate/2026-10-19/metrics.json", json.dumps(metrics(mae=9)).encode())
    result = gate.gate(store, "2026-10-19")
    assert result == {
        "run_id": "2026-10-19",
        "promoted": False,
        "reason": "candidate MAE 9.000 does not beat the yesterday baseline 1.500",
        "model_available": False,
        "candidate_mae": 9,
        "baseline_mae": 1.5,
        "champion_mae": None,
    }
