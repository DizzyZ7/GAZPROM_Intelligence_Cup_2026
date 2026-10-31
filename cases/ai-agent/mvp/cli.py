"""Offline demo CLI; explicit review, no auto-delivery."""
import argparse
import json
from pathlib import Path
from pulse import Ticket, analyze
from store import SignalStore

def main():
    parser = argparse.ArgumentParser(description='ТИИм Pulse demo')
    parser.add_argument('--db', default='pulse-demo.sqlite3')
    sub = parser.add_subparsers(dest='command', required=True)
    imp = sub.add_parser('import')
    imp.add_argument('--file', default=str(Path(__file__).with_name('sample_tickets.json')))
    ls = sub.add_parser('list')
    ls.add_argument('--status', default=None)
    review = sub.add_parser('review')
    review.add_argument('signal_id')
    review.add_argument('--action', choices=['approved', 'rejected'], required=True)
    review.add_argument('--actor', required=True)
    review.add_argument('--reason', required=True)
    audit = sub.add_parser('audit')
    audit.add_argument('signal_id')
    args = parser.parse_args()
    store = SignalStore(args.db)
    try:
        if args.command == 'import':
            data = json.loads(Path(args.file).read_text(encoding='utf-8'))
            result = analyze([Ticket(**x) for x in data])
            print(json.dumps({'processed': result['processed'], 'signals_found': len(result['signals']),
                              'new_signals': store.ingest(result['signals'])}, ensure_ascii=False))
        elif args.command == 'list':
            print(json.dumps(store.list_signals(args.status), ensure_ascii=False, indent=2))
        elif args.command == 'review':
            store.review(args.signal_id, args.action, args.actor, args.reason)
            print('Review recorded')
        elif args.command == 'audit':
            print(json.dumps(store.audit(args.signal_id), ensure_ascii=False, indent=2))
    finally:
        store.close()

if __name__ == '__main__':
    main()
