"""Генерация и проверка контрольного набора из командной строки."""

import argparse
import json

from .generator import DEFAULT_SEED, generate_dataset, write_dataset
from .validation import load_dataset, validate_dataset


def main():
    parser = argparse.ArgumentParser(description="Контрольные обращения ТИИм Pulse")
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("generate", help="Создать новый набор")
    generate.add_argument("--size", type=int, default=500)
    generate.add_argument("--seed", type=int, default=DEFAULT_SEED)
    generate.add_argument("--output", required=True)
    validate = sub.add_parser("validate", help="Проверить контрольные суммы и разметку")
    validate.add_argument("--directory", required=True)
    args = parser.parse_args()
    try:
        if args.command == "generate":
            bundle = generate_dataset(args.size, args.seed)
            write_dataset(bundle, args.output)
        else:
            bundle = load_dataset(args.directory)
        print(json.dumps(validate_dataset(bundle), ensure_ascii=False))
    except (ValueError, FileExistsError) as exc:
        parser.exit(2, f"Ошибка: {exc}\n")


if __name__ == "__main__":
    main()
