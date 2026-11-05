"""Безопасные JSON/CSV-артефакты и атомарная запись новых каталогов."""

import csv
import hashlib
import io
import json
import tempfile
from pathlib import Path


def json_bytes(value):
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode()


def sha256(content):
    return hashlib.sha256(content).hexdigest()


def read_json(path):
    try:
        return json.loads(Path(path).read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Не удалось прочитать JSON-артефакт") from exc


def csv_bytes(fields, rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                k: (
                    "'" + v
                    if isinstance(v, str)
                    and v.startswith(("=", "+", "-", "@", "\t", "\r", "\n"))
                    else v
                )
                for k, v in row.items()
            }
        )
    return stream.getvalue().encode()


def write_directory(output, files):
    output = Path(output)
    if output.exists():
        raise FileExistsError("Выходной каталог уже существует")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="pulse-benchmark-", dir=output.parent
    ) as temporary:
        stage = Path(temporary) / "content"
        stage.mkdir()
        for name, content in files.items():
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Недопустимый путь артефакта")
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        stage.rename(output)
