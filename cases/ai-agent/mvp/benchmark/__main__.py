"""CLI с раздельными калибровкой, финальной оценкой и проверкой артефактов."""

import argparse
import json
from urllib.error import URLError

from .protocol import calibrate, evaluate, local_ollama, verify_report


def main():
    parser = argparse.ArgumentParser(description="Независимый benchmark ТИИм Pulse")
    sub = parser.add_subparsers(dest="command", required=True)
    tune = sub.add_parser("tune", help="Выбрать пороги только на validation")
    final = sub.add_parser("evaluate", help="Оценить holdout по готовому протоколу")
    for command in (tune, final):
        command.add_argument("--dataset", required=True)
        command.add_argument("--output", required=True)
        command.add_argument("--ollama-model")
    final.add_argument("--calibration", required=True)
    verify = sub.add_parser(
        "verify", help="Проверить сохраненные отчеты без нового инференса"
    )
    verify.add_argument("--directory", required=True)
    args = parser.parse_args()
    try:
        if args.command == "verify":
            result = verify_report(args.directory)
        else:
            provider, descriptor, reason = None, None, None
            if args.ollama_model:
                try:
                    provider, descriptor = local_ollama(args.ollama_model)
                except (ValueError, OSError, URLError) as exc:
                    if args.command == "evaluate":
                        raise ValueError(
                            "Зафиксированный embedding-provider недоступен"
                        ) from exc
                    reason = (
                        "Локальная модель недоступна; проверка не выполнена: "
                        + type(exc).__name__
                    )
            if args.command == "tune":
                result = calibrate(
                    args.dataset, args.output, provider, descriptor, reason
                )
            else:
                result = evaluate(
                    args.dataset, args.calibration, args.output, provider, descriptor
                )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(
            2,
            "Ошибка benchmark: "
            + type(exc).__name__
            + "; проверьте схему, контрольные суммы и готовность провайдера.\n",
        )


if __name__ == "__main__":
    main()
