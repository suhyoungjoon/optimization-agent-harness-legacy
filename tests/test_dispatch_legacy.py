"""dispatch 레가시 형식(I/F별 CSV) 내보내기·읽기: 왕복이 같은 인스턴스·정답표를 만들고, 잘못된 입력은 파일·행으로 알린다."""

import csv
import json

import pytest

from domains.dispatch import legacy
from domains.dispatch.generator import generate
from domains.dispatch.pack import get_pack

SEED = 42


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    inst, truth = generate(SEED, ["P1", "P5"])
    out = tmp_path_factory.mktemp("legacy") / "sample"
    legacy.export(inst, truth, out)
    return inst, truth, out


def test_export_writes_one_csv_per_interface(exported):
    _, _, out = exported
    assert sorted(p.name for p in out.iterdir()) == [
        "bookings.csv", "capacity.csv", "manifest.json", "orders.csv", "skills.csv", "std_time.csv",
        "truth.json", "workers.csv"]
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "dispatch-legacy" and manifest["days"] == 10
    with (out / "capacity.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert set(rows[0]) == {"worker_id", "date", "slot", "available"}
    assert {r["available"] for r in rows} == {"Y", "N"}
    assert all(r["slot"][-2:] in ("00", "30") for r in rows)          # 30분 칸


def test_round_trip_gives_same_instance_and_truth(exported):
    inst, truth, out = exported
    loaded, loaded_truth = legacy.load_dir(out)
    assert loaded == inst
    assert loaded_truth == json.loads(json.dumps(truth))


def test_round_trip_solves_the_same(exported):
    inst, _, out = exported
    pack = get_pack()
    loaded, _ = legacy.load_dir(out)
    assert pack.solve(loaded, pack.params) == pack.solve(inst, pack.params)


def test_truth_is_optional(exported, tmp_path):
    _, _, out = exported
    copy = tmp_path / "no_truth"
    copy.mkdir()
    for p in out.iterdir():
        if p.name != "truth.json":
            (copy / p.name).write_bytes(p.read_bytes())
    _, truth = legacy.load_dir(copy)
    assert truth == {}


def _rewrite(src, dst, filename, edit):
    dst.mkdir()
    for p in src.iterdir():
        (dst / p.name).write_bytes(p.read_bytes())
    with (dst / filename).open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    edit(rows)
    with (dst / filename).open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


@pytest.mark.parametrize("filename, edit, words", [
    ("orders.csv", lambda rows: rows[2].update(branch="Z"), ["orders.csv", "4행", "branch"]),
    ("orders.csv", lambda rows: rows[0].update(work_type="paint"), ["orders.csv", "2행", "work_type"]),
    ("orders.csv", lambda rows: rows[1].update(desired_time="9시"), ["orders.csv", "3행", "desired_time"]),
    ("orders.csv", lambda rows: rows[1].update(desired_date="2030-01-01"), ["orders.csv", "기간"]),
    ("orders.csv", lambda rows: rows[1].update(order_id=rows[0]["order_id"]), ["orders.csv", "중복"]),
    ("capacity.csv", lambda rows: rows[0].update(slot="09:15"), ["capacity.csv", "30분"]),
    ("capacity.csv", lambda rows: rows[0].update(available="X"), ["capacity.csv", "available"]),
    ("skills.csv", lambda rows: rows[0].update(worker_id="W??"), ["skills.csv", "작업자"]),
    ("bookings.csv", lambda rows: rows[0].update(order_id="NOPE"), ["bookings.csv", "지시서"]),
    ("std_time.csv", lambda rows: rows[0].update(minutes="-5"), ["std_time.csv", "minutes"]),
])
def test_invalid_rows_name_file_and_line(exported, tmp_path, filename, edit, words):
    _, _, out = exported
    bad = tmp_path / "bad"
    _rewrite(out, bad, filename, edit)
    with pytest.raises(ValueError) as err:
        legacy.load_dir(bad)
    assert all(w in str(err.value) for w in words), str(err.value)


@pytest.mark.parametrize("name", ["../etc", "a/b", "", ".", "x y"])
def test_source_name_must_be_a_plain_folder_name(name):
    with pytest.raises(ValueError):
        legacy.resolve(name)


def test_pack_load_reads_named_folder(exported, monkeypatch):
    inst, truth, out = exported
    monkeypatch.setenv("LEGACY_DATA_DIR", str(out.parent))
    loaded, loaded_truth = get_pack().load("sample")
    assert loaded == inst and loaded_truth["faults"].keys() == truth["faults"].keys()
    with pytest.raises(ValueError):
        get_pack().load("missing")
