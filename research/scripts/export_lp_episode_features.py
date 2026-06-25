"""Export derived LP episode features with receipt-backed gas fields."""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import asdict, fields
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from research.backtester.lp_episode_features import (  # noqa: E402
    LPEpisodeFeature,
    build_lp_episode_features,
)
from research.scripts.export_lp_paper_episodes import _read_ledger_rows  # noqa: E402


def export_lp_episode_features(
    ledger_path: Path,
    receipts_path: Path,
    output_path: Path,
    *,
    native_token_usd: Decimal | None = None,
    native_price_path: Path | None = None,
    native_price_max_age_ms: int | None = None,
) -> int:
    features = build_lp_episode_features(
        _read_ledger_rows(ledger_path),
        _read_receipt_rows(receipts_path),
        native_token_usd=native_token_usd,
        native_price_rows=_read_native_price_rows(native_price_path)
        if native_price_path is not None
        else None,
        native_price_max_age_ms=native_price_max_age_ms,
    )
    _write_feature_rows(output_path, features)
    return len(features)


def _read_receipt_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"empty receipt CSV: {path}")
        required = {"chain", "tx_hash", "native_fee_wei"}
        missing = sorted(required.difference(reader.fieldnames))
        if missing:
            raise ValueError(f"receipt CSV missing fields {missing}: {path}")
        return list(reader)


def _read_native_price_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"empty native price CSV: {path}")
        required = {"chain", "timestamp_ms", "native_token_usd", "source"}
        missing = sorted(required.difference(reader.fieldnames))
        if missing:
            raise ValueError(f"native price CSV missing fields {missing}: {path}")
        return list(reader)


def _write_feature_rows(output_path: Path, features: list[LPEpisodeFeature]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [field.name for field in fields(LPEpisodeFeature)]
    with output_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for feature in features:
            writer.writerow(_csv_row(feature))


def _csv_row(feature: LPEpisodeFeature) -> dict[str, object]:
    return {
        key: "" if value is None else str(value) if isinstance(value, Decimal) else value
        for key, value in asdict(feature).items()
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--receipts", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    price_group = parser.add_mutually_exclusive_group()
    price_group.add_argument("--native-token-usd", type=Decimal)
    price_group.add_argument("--native-price-csv", type=Path)
    parser.add_argument("--native-price-max-age-ms", type=int)
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    if args.native_price_csv is not None and args.native_price_max_age_ms is None:
        raise SystemExit("--native-price-csv requires --native-price-max-age-ms")
    if args.native_price_csv is None and args.native_price_max_age_ms is not None:
        raise SystemExit("--native-price-max-age-ms requires --native-price-csv")
    count = export_lp_episode_features(
        args.ledger,
        args.receipts,
        args.out,
        native_token_usd=args.native_token_usd,
        native_price_path=args.native_price_csv,
        native_price_max_age_ms=args.native_price_max_age_ms,
    )
    print(f"wrote {count} LP episode feature rows to {args.out}")


if __name__ == "__main__":
    main()
