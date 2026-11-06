"""Командная строка демонстрационного конвейера."""
import argparse
import json
from pathlib import Path
from evidence import get_signal
from service import import_tickets
from store import SignalStore

def main():
    parser = argparse.ArgumentParser(description="ТИИм Pulse")
    parser.add_argument("--db", default="pulse-demo.sqlite3")
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import")
    imp.add_argument("--file", default=str(Path(__file__).with_name("sample_tickets.json")))
    imp.add_argument("--mode", choices=["rules", "semantic"], default="rules")
    imp.add_argument("--threshold", type=float, default=0.86)
    ls = sub.add_parser("list")
    ls.add_argument("--status")
    show = sub.add_parser("show")
    show.add_argument("signal_id")
    review = sub.add_parser("review")
    review.add_argument("signal_id")
    review.add_argument("--action", choices=["approved", "rejected"], required=True)
    review.add_argument("--actor", required=True)
    review.add_argument("--reason", required=True)
    review.add_argument("--revision", required=True, help="Версия карточки из команды show")
    audit = sub.add_parser("audit")
    audit.add_argument("signal_id")
    args = parser.parse_args()
    if args.command == "import":
        if args.mode == "semantic":
            parser.error("Семантический режим доступен через service.import_tickets с явным провайдером; локальная модель еще не подключена")
        print(json.dumps(import_tickets(args.db,args.file,mode=args.mode,threshold=args.threshold),ensure_ascii=False))
        return
    store = SignalStore(args.db)
    try:
        if args.command == "list":
            print(json.dumps(store.list_signals(args.status),ensure_ascii=False,indent=2))
        elif args.command == "show":
            print(json.dumps(get_signal(store, args.signal_id), ensure_ascii=False, indent=2))
        elif args.command == "review":
            store.review(args.signal_id, args.action, args.actor, args.reason,
                         expected_revision=args.revision)
            print("Решение сохранено")
        elif args.command == "audit":
            print(json.dumps(store.audit(args.signal_id),ensure_ascii=False,indent=2))
    except KeyError:
        parser.error("Сигнал не найден")
    except ValueError as exc:
        parser.error(str(exc))
    finally:
        store.close()

if __name__ == "__main__":
    main()
