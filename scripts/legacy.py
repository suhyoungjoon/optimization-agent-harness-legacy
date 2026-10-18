"""dispatch 레가시 형식(I/F별 CSV) 내보내기·등록 CLI.

python -m scripts.legacy export --seed 42 --faults P1,P4 --name mydata   # 가상 데이터를 data/legacy/mydata 로
python -m scripts.legacy load mydata                                      # data/legacy/mydata 를 데이터셋으로 등록
"""

import argparse
import os

from api.main import DEFAULT_DB
from core.registry import load_pack
from core.storage.store import Store
from domains.dispatch import legacy


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("export", help="생성기로 만든 가상 데이터를 레가시 형식 폴더로 내보낸다")
    ex.add_argument("--seed", type=int, required=True)
    ex.add_argument("--faults", default="", help="쉼표로 구분한 결함 ID (예: P1,P4). 정답표는 truth.json")
    ex.add_argument("--name", required=True, help="폴더 이름 (data/legacy/<이름>)")
    ex.add_argument("--no-truth", action="store_true", help="truth.json을 쓰지 않는다 (분석 채점 없음)")
    ld = sub.add_parser("load", help="레가시 형식 폴더를 읽어 데이터셋으로 등록한다")
    ld.add_argument("name", help="폴더 이름 (data/legacy/<이름>)")
    ld.add_argument("--db", default=os.environ.get("HARNESS_DB", str(DEFAULT_DB)))
    args = parser.parse_args(argv)

    pack = load_pack("dispatch")
    if args.command == "export":
        faults = [f for f in args.faults.split(",") if f]
        instance, truth = pack.generate(args.seed, faults)
        root = os.environ.get("LEGACY_DATA_DIR", str(legacy.REPO_DIR / "data" / "legacy"))
        if not legacy._NAME.match(args.name):
            parser.error(f"이름은 영문·숫자·_·-만: {args.name}")
        out = legacy.export(instance, None if args.no_truth else truth, os.path.join(root, args.name))
        print(f"exported: {out}  orders: {len(instance.orders)}  booked: {len(instance.fixed)}  "
              f"backlog: {len(pack.items(instance))}")
    else:
        try:
            instance, truth = pack.load(args.name)
        except ValueError as exc:
            parser.exit(1, f"error: {exc}\n")
        dataset = Store(args.db).save_dataset("dispatch", 0, [], len(pack.items(instance)), source=args.name)
        print(f"dataset: {dataset['id']}  items: {dataset['items']}  truth: {'있음' if truth else '없음'}  db: {args.db}")


if __name__ == "__main__":
    main()
