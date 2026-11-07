"""Извлечение намерения, сценария и наблюдаемого симптома из текста."""

import re
import unicodedata
from dataclasses import dataclass

from pulse import redact

ANALYSIS_VERSION = "context-v2.2"
ACTIONABLE = {"bug", "feature_request"}
_LOOKALIKE_WORD = re.compile(r"\b[а-яabcehkmoptxy]+\b")
_CYRILLIC = re.compile(r"[а-я]")
_LOOKALIKES = str.maketrans("abcehkmoptxy", "авсенкмортху")


def _phrase(pattern):
    return re.compile(rf"\b(?:{pattern})")


_INSTRUCTION = re.compile(
    r"\[(?:system|assistant|developer|tool)\]|<(?:system|assistant|developer|instruction|tool)\b|"
    r"\b(?:игнорир\w*|забуд\w*|обойди)\s+(?:все\s+)?(?:предыдущ|систем|инструкц|правил)|"
    r"\b(?:вызови|выполни)\s+(?:функци|инструмент|команд)|"
    r"\b(?:ignore|override)\s+(?:all\s+)?(?:(?:your|the)\s+)?(?:previous|system)\s+instructions\b"
)
_ABSENT_ERROR = _phrase(
    r"(?:без|нет)\s+(?:ошиб\w*|сбо\w*)(?:\s+(?:http\s*)?[45]\d{2}\b)?|"
    r"(?:ошиб\w*|сбо\w*)(?:\s+(?:http\s*)?[45]\d{2}\b)?\s+(?:нет|не\s+было)|"
    r"не\s+(?:выда\w*|показыва\w*)\s+ошиб\w*(?:\s+[45]\d{2}\b)?"
)
_HYPOTHETICAL = _phrase(r"если\s+(?:появит|возникн|будет|покажет)[^.!?;\n]*")
_CONDITION = _phrase(r"если\s+[^.!?;\n]*")
_RESOLVED = _phrase(
    r"(?:уже|сейчас|теперь)\s+(?:все\s+)?(?:работает|исправлено|решено)"
)
_NEGATION_BEFORE = re.compile(r"\b(?:не|без|нет)[ \t]+$")
_NEGATION_AFTER = re.compile(
    r"[ \t]+(?:(?:больше|уже|сейчас)[ \t]+)?(?:нет\b|не\s+(?:наблюда|появля|возника))"
)
_OPERATION_NEGATION_BEFORE = re.compile(
    r"\b(?:не\s+(?:при|во\s+время)|"
    r"не\s+(?:связан\w*|относ\w*)\s+(?:с|со|к))\s+"
    r"(?:(?:процедур|операци)\w*\s+)?$"
)
_OPERATION_NEGATION_AFTER = re.compile(
    r"\s+(?:(?:тут|здесь)\s+)?(?:ни\s+при\s+чем|не\s+(?:связан\w*|затронут\w*))\b"
)
_WORD_END = re.compile(r"\w*")
_HTTP_CODE = _phrase(
    r"(?:http(?:/\d(?:\.\d)?)?\s*(?:status\s*)?|"
    r"ошибк\w*\s*|статус\s+|код\s+ошибк\w*\s*|ответ\s+сервер\w*\s*)"
    r"[:№#=()–—-]?\s*([45]\d{2})\b"
)
_HTTP_RESULT = _phrase(
    r"(?:(?:открывается|появляется)\s+|"
    r"(?:открывается|появляется|возвращает|выдает|получаю)\s+"
    r"страниц[ау]\s+с\s+кодом\s+)"
    r"([45]\d{2})(?=[ \t]*(?:\.(?!\d)|[!?;\n]|$))"
)
_APP_CODE = _phrase(
    r"(?:ошибк\w*|код\s+ошибк\w*|error)\s*[:№#=()–—-]?\s*"
    r"([a-z]{1,8}[-_]?\d{2,8}|[a-z][a-z0-9]{0,15}(?:_[a-z0-9]{1,15}){1,3})\b"
)
_HOW_TO = _phrase(
    r"(?:как|где|каким\s+образом)\s+(?:скач|выгруз|найти|включ|выключ|добав|измен|"
    r"восстанов|заброни|получи|запис|настро|оформ|закры|войти|прикреп|подключ|"
    r"посмотр|увид|сохран|провер|отмен|перенес|сброс|оплат|подпис|выбр)|"
    r"(?:нужна|ищу)\s+(?:инструкц|справк)|подскажите[,:]?\s+последовательность\s+действий"
)
_GUIDANCE_OBJECT = _phrase(r"инструкц|справк|руководств")
_SERVICE = _phrase(
    r"(?:выдайте|выдать|предостав\w*|назнач\w*|снимите|снять|сброс(?:ь(?:те)?|ить)|"
    r"закройте|закрыть|смен(?:и(?:те)?|ить)|перенесите|перенести)\s+"
    r"(?:(?:мне|мой|мою|новый|новую|временный)\s+){0,2}"
    r"(?:доступ|прав[ао]\b|роль|парол|тикет|обращен|заявк|владельц|ответственн)|"
    r"нуж(?:ен|на|ны)\s+(?:доступ\s+к\b|роль\b|права\s+на\b)|"
    r"(?:прошу|нуж(?:ен|на)|требуется)\s+(?:сброс(?:а)?|смен(?:а|ы))\s+парол"
)
_FEATURE = _phrase(
    r"хотелось\s+бы|хочу\s+(?:возможност|чтобы)|нужн[ао]\s+(?:возможност|функц|кнопк)|"
    r"добавьте|добавить\s+возможност|можно\s+ли\s+добавить|сделайте\s+так,?\s+чтобы|"
    r"предлагаю\s+(?:добав|введ|сдела|расшир)|не\s+хватает\s+(?:функц|возможност|кнопк|напоминан)|"
    r"было\s+бы\s+(?:удобно|полезно)|хорошо\s+бы|есть\s+пожелание|просьба\s+разрешить|"
    r"нуж(?:ен|на|ны)\s+(?:второй|дополнительный)\s+(?:контакт|номер|телефон)"
)
_OTHER = _phrase(
    r"коммерческ\w*\s+предложен|предлагаем\s+услуг|для\s+сведения|"
    r"планов\w*\s+(?:техработ|работ)|техническ\w*\s+работ\w*|от\s+сервиса\s+ничего\s+не\s+требуется"
)
_GRATITUDE = re.compile(r"^(?:спасибо|благодарю)\b[^.!?\n]*[.!?\s]*$")
_FAILURE = _phrase(
    r"ошибк|сбой|сбои|сломал|не\s+(?:работа|получа|удае|проход|пуска|удалось|смог|могу)|"
    r"отказыва|отклоня|неверн|некорректн|неправильн|недоступ|выбрасыва|вылета|"
    r"падает|падение|белый\s+экран|не\s+поддержива"
)

_DOMAINS = {
    "booking_queue": _phrase(
        r"брон(?:ь|ир)|талон|очеред|слот|записаться|окн[оа]\s+(?:запис|прием)|"
        r"(?:отмен\w*|перенос\w*|перенес\w*)\s+запис"
    ),
    "contract": _phrase(r"договор|оферт"),
    "vehicle_registry": _phrase(
        r"автомобил|автопарк|транспортн\w*\s+средств|машин(?:а|ы|е|у|ой|ами|ах)\b|"
        r"госномер|vin\b|тс\b"
    ),
    "sms_recovery": _phrase(
        r"смс\b|sms\b|код\s+(?:подтвержден|восстановлен|сброс|из\s+сообщен)|"
        r"восстанов\w*\s+парол"
    ),
    "contact_edit": _phrase(
        r"контактн\w*\s+(?:номер|телефон)|контакт(?:ы|ов|ами|ах)\b|"
        r"(?:телефон|номер)\w*\s+(?:в\s+)?(?:профил|контакт)|"
        r"(?:смен\w*|помен\w*|измен\w*|обнов\w*)\s+(?:номер|телефон)"
    ),
    "report": _phrase(r"отчет|выгруз|экспорт|csv\b|xlsx\b|сводк"),
    "payment": _phrase(
        r"оплат|платеж|списан|списал|эквайр|возврат\w*\s+(?:средств|денег)|чек\w*\b"
    ),
    "account_access": _phrase(r"вход|войти|логин|авторизац|аутентификац|сесси|парол"),
    "attachments": _phrase(r"вложен|прикреп|загруз\w*\s+(?:новый\s+)?файл"),
    "notifications": _phrase(r"уведомлен|оповещен|push\b|пуш\b|рассылк"),
    "search": _phrase(r"поиск|поисков|результат\w*\s+поиск|фильтр"),
    "permissions": _phrase(
        r"доступ\w*\s+(?:к|для)\b|прав[ао]\b|полномочи|рол(?:ь|и|ями|ях)\b"
    ),
    "appearance": _phrase(
        r"темн\w*\s+тем|ночн\w*\s+режим|контраст|цвет\w*\s+интерфейс"
    ),
    "accessibility": _phrase(
        r"шрифт|масштабирован|озвуч|скринридер|экранн\w*\s+диктор"
    ),
    "account_owner": _phrase(
        r"владелец|владельц|собственник|ответственн\w*\s+сотрудник"
    ),
    "ticket_lifecycle": _phrase(r"(?:закры\w*|архив\w*)\s+(?:тикет|заявк|обращен)"),
}
_DOWNLOAD = _phrase(
    r"скач|загрузк\w*\s+готов|сохран\w*\s+(?:готов\w*\s+)?(?:отчет|договор)"
)
_EXPORT = _phrase(r"экспорт|выгруз")
_CONTRACT_NEXT = _phrase(r'кнопк\w*\s+[«"]?далее\b|следующ\w*\s+шаг')
_SUPPORTING_ATTACHMENT = _phrase(
    r"(?:прикрепил\w*|приложил\w*|прилагаю)\s+(?:скрин|лог|снимок)"
)

_SYMPTOMS = {
    "timeout": _phrase(
        r"тайм[- ]?аут|timeout\b|превыш\w*\s+время\s+ожидан|время\s+ожидан\w*\s+истек"
    ),
    "stall": _phrase(
        r"завис|бесконечн\w*\s+(?:груз|загруз|крут|ожидан)|крутится\s+(?:индикатор|спиннер)"
    ),
    "control_unresponsive": _phrase(
        r"(?:кнопк\w*|нажима\w*).{0,55}(?:не\s+реагир|неактив|ничего\s+не\s+происход)|"
        r"неактивн\w*\s+кнопк|не\s+реагир\w*.{0,25}кнопк"
    ),
    "access_denied": _phrase(
        r"доступ\s+(?:запрещ|отклон)|нет\s+прав|недостаточн\w*\s+прав|отказ\w*\s+в\s+доступ"
    ),
    "missing_delivery": _phrase(
        r"не\s+(?:приход|пришел|пришла|пришло|пришли|достав)|"
        r"не\s+получи(?:л|ла|ли)\s+(?:смс\b|sms\b|код|сообщен|уведомлен)|"
        r"(?:уведомлен\w*|смс|sms|код|сообщен\w*)\s+(?:так\s+и\s+)?нет\b"
    ),
    "expired": _phrase(
        r"просроч|устарел\w*\s+код|срок\w*(?:\s+действия)?\s+истек|код\s+истек|сессия\s+истек"
    ),
    "duplicate": _phrase(
        r"дубликат|дублир|задво|повторно\s+спис|двойн\w*\s+(?:платеж|запис|спис)|"
        r"(?:дважды|два\s+раза)\s+(?:спис|приш|появ|созда)|"
        r"(?:списал|пришел|пришла|пришло|пришли|появил|создал)\w*\s+(?:дважды|два\s+раза)|"
        r"(?:две\s+оплат\w*|два\s+(?:платеж\w*|списан\w*))\s+"
        r"после\s+одного\s+подтвержден\w*\s+оплат"
    ),
    "missing_record": _phrase(
        r"пропал|исчез|не\s+отобража|не\s+видно|потерял\w*\s+(?:талон|запис|вложен)"
    ),
    "empty_result": _phrase(
        r"пуст\w*\s+(?:отчет|таблиц|файл|спис|лист|результат)|(?:отчет|файл|выгрузк)\w*\s+пуст|"
        r"(?:ноль|0)\s+строк|нет\s+результат|результат\w*\s+нет"
    ),
    "encoding": _phrase(
        r"кракозябр|иероглиф|(?:сбил\w*|бит\w*|неверн\w*)\s+кодировк|"
        r"вопросительн\w*\s+знак\w*\s+вместо\s+букв"
    ),
    "unsupported_format": _phrase(
        r"формат\w*\s+не\s+поддержива|неподдержива\w*\s+формат|недопустим\w*\s+формат"
    ),
    "size_limit": _phrase(
        r"слишком\s+больш\w*\s+файл|файл\s+слишком\s+больш|превыш\w*\s+(?:лимит|размер)|лимит\w*\s+размер"
    ),
    "truncated": _phrase(
        r"обреза|не\s+все\s+(?:строк|столбц|страниц)|только\s+первые\s+\d+\s+строк"
    ),
    "wrong_amount": _phrase(
        r"сумм\w*.{0,32}(?:не\s+совпада|отлича|неверн)|неверн\w*\s+сумм"
    ),
    "wrong_time": _phrase(
        r"неверн\w*\s+(?:дат|врем)|врем\w*.{0,30}(?:не\s+совпада|сдвиг)|часов\w*\s+пояс\w*.{0,22}сбива|"
        r"(?:раньше|позже)\s+выбранн\w*\s+времени"
    ),
}
_ACTION_FAILURES = {
    "open_failed": _phrase(r"не\s+(?:открыва|открыл)|невозможно\s+откры"),
    "download_failed": _phrase(
        r"не\s+(?:скачива|скачал)|не\s+могу\s+скач|не\s+удал\w*\s+скач"
    ),
    "upload_failed": _phrase(
        r"не\s+(?:загружа|загрузил|прикрепля|прикрепил)|не\s+уда\w*\s+прикреп"
    ),
    "save_failed": _phrase(r"не\s+(?:сохраня|сохранил)|не\s+уда\w*\s+сохран"),
    "send_failed": _phrase(r"не\s+(?:отправля|отправил)|не\s+уда\w*\s+отправ"),
    "generation_failed": _phrase(
        r"не\s+(?:формиру|сформиру|сформировал)|не\s+уда\w*\s+сформир"
    ),
    "login_failed": _phrase(
        r"не\s+могу\s+войти|не\s+пуска\w*\s+в\s+(?:аккаунт|кабинет)|не\s+уда\w*\s+войти"
    ),
    "rejected_input": _phrase(
        r"не\s+принима|отклоня\w*\s+(?:код|платеж|файл|парол)|(?:код|парол)\w*\s+неверн"
    ),
}
_CAPABILITIES = {
    "schedule": _phrase(
        r"расписан|ежеднев|еженедел|регулярн\w*\s+выгруз|автоматическ\w*\s+(?:выгруз|отчет)"
    ),
    "multiple_contacts": _phrase(
        r"(?:несколько|два|втор\w*|дополнительн\w*)\s+(?:контакт|телефон|номер)"
    ),
    "edit_contact": _phrase(
        r"(?:редактир|менять|поменя|измен|смен)\w*\s+(?:(?:свой|контактный)\s+)?(?:телефон|номер|контакт)"
    ),
    "dark_theme": _phrase(r"темн\w*\s+тем|ночн\w*\s+режим"),
    "font_scale": _phrase(
        r"(?:размер\w*\s+шрифт|масштабирован\w*\s+текст|увелич\w*\s+шрифт)"
    ),
    "preview": _phrase(r"предпросмотр|предварительн\w*\s+просмотр"),
    "bulk_actions": _phrase(
        r"массов\w*\s+(?:действи|редактир|загруз|выбор|удал)|пакетн\w*\s+(?:загруз|обработ|редактир)"
    ),
    "select_columns": _phrase(
        r"(?:выбор|выбира\w*|настрой\w*)\s+(?:своих\s+)?(?:столбц|колонок)|свои\s+колонк"
    ),
    "notification_preferences": _phrase(
        r"(?:настраива\w*|отключа\w*|включа\w*)\s+(?:лишн\w*\s+)?(?:уведомлен|оповещен|рассылк)"
    ),
    "notification_delivery": _phrase(
        r"(?:получать|присыла\w*|приходи\w*)\s+(?:push\s+|смс\s+)?(?:уведомлен|оповещен)|напоминан"
    ),
}
_FORMATS = _phrase(r"(csv|xlsx|pdf|json|xml|png|jpeg|zip)\b")
_OPERATIONS = {
    "report_export": {
        "generate": _phrase(
            r"формир|сформир|генерац|сгенерир|(?:созда|подготов)\w*\s+отчет"
        ),
        "export": _EXPORT,
        "view": _phrase(
            r"просмотр|откры\w*\s+(?:готов\w*\s+)?отчет|отчет\w*\s+не\s+откры"
        ),
    },
    "booking_queue": {
        "cancel": _phrase(r"отмен"),
        "reschedule": _phrase(r"перенос|перенес|перенест"),
    },
    "payment": {
        "refund": _phrase(r"возврат|вернуть\s+(?:деньг|средств)"),
        "receipt": _phrase(r"чек\w*\b|квитанц"),
    },
    "account_access": {
        "login": _phrase(r"вход(?:а|е|у|ом)?\b|войти\b|авторизац|аутентификац"),
        "password_reset": _phrase(r"(?:сброс|восстановлен)\w*\s+парол"),
    },
    "attachments": {
        "upload": _phrase(r"прикреп|загруз\w*\s+(?:новый\s+)?файл"),
        "download": _DOWNLOAD,
        "remove": _phrase(r"удал\w*\s+(?:файл|вложен)"),
        "open": _phrase(r"откры\w*\s+(?:файл|вложен)|вложен\w*\s+не\s+откры"),
    },
    "vehicle_registry": {
        "save": _phrase(r"сохран|добав|зарегистрир"),
        "remove": _phrase(r"удал"),
        "edit": _phrase(r"редактир|измен|обнов"),
        "view": _phrase(r"просмотр|отобража|пропал|исчез"),
    },
    "contract": {
        "open": _phrase(r"откры|просмотр"),
        "sign": _phrase(r"подпис"),
    },
}
_OPERATION_REQUIRED = {
    "attachments",
    "report_export",
    "vehicle_registry",
    "contract",
    "account_access",
}


@dataclass(frozen=True)
class TicketContext:
    category: str
    workflow: str = "unclassified"
    operation: str | None = None
    symptom: str | None = None
    reason: str = "insufficient_context"

    @property
    def groupable(self):
        return self.reason == "context_resolved"


def sanitized_text(text):
    return redact(unicodedata.normalize("NFKC", text))


def _matches(text, rules):
    return [name for name, rule in rules.items() if rule.search(text)]


def _workflow(text):
    scopes = set(_matches(_SUPPORTING_ATTACHMENT.sub(" ", text), _DOMAINS))
    if "sms_recovery" in scopes:
        scopes.discard("account_access")
    if len(scopes) != 1:
        return "unclassified", "ambiguous_workflow" if scopes else "missing_workflow"
    scope = scopes.pop()
    if scope == "report":
        scope = (
            "report_download"
            if _DOWNLOAD.search(text) and not _EXPORT.search(text)
            else "report_export"
        )
    elif scope == "contract":
        if _DOWNLOAD.search(text):
            scope = "contract_download"
        elif _CONTRACT_NEXT.search(text):
            scope = "contract_next_button"
    return scope, None


def _observations(
    text, pattern, *, negation_before=_NEGATION_BEFORE, negation_after=_NEGATION_AFTER
):
    for match in pattern.finditer(text):
        start = match.start()
        end = _WORD_END.match(text, match.end()).end()
        if not negation_before.search(
            text, max(0, start - 64), start
        ) and not negation_after.match(text, end):
            yield match


def _problem(text):
    codes = {
        f"http_{match[1]}"
        for pattern in (_HTTP_CODE, _HTTP_RESULT)
        for match in _observations(text, pattern)
    }
    codes.update(f"app_{match[1]}" for match in _observations(text, _APP_CODE))
    symptoms = [
        name
        for name, rule in _SYMPTOMS.items()
        if next(_observations(text, rule), None)
    ]
    actions = [
        name
        for name, rule in _ACTION_FAILURES.items()
        if next(_observations(text, rule), None)
    ]
    observed = sorted(codes) + symptoms or actions
    return bool(observed or next(_observations(text, _FAILURE), None)), observed


def _operations(text, workflow, category):
    rules = _OPERATIONS.get(workflow, {})
    if workflow != "account_access":
        return _matches(text, rules)
    operations = set()
    for clause in re.split(r"[!?;\n]+|\.(?!\d)", text):
        if category == "feature_request" or _problem(clause)[0]:
            operations.update(
                operation
                for operation, rule in rules.items()
                if next(
                    _observations(
                        clause,
                        rule,
                        negation_before=_OPERATION_NEGATION_BEFORE,
                        negation_after=_OPERATION_NEGATION_AFTER,
                    ),
                    None,
                )
            )
    return sorted(operations)


def classify_context(text):
    normalized = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    normalized = _LOOKALIKE_WORD.sub(
        lambda match: (
            match[0].translate(_LOOKALIKES) if _CYRILLIC.search(match[0]) else match[0]
        ),
        normalized,
    )
    if _INSTRUCTION.search(normalized):
        return TicketContext("unknown", reason="untrusted_instruction")

    observed = _HYPOTHETICAL.sub(" ", _ABSENT_ERROR.sub(" ", normalized))
    observed = _CONDITION.sub(
        lambda match: " " if _HTTP_RESULT.search(match[0]) else match[0], observed
    )
    resolved = _RESOLVED.search(observed)
    if resolved and not _problem(observed[resolved.end() :])[0]:
        observed = observed[resolved.end() :]
    failure, symptoms = _problem(observed)
    service = _SERVICE.search(normalized)
    feature = _FEATURE.search(normalized)
    guidance = _HOW_TO.search(normalized)
    workflow, scope_reason = _workflow(observed)

    if (
        guidance
        and not feature
        and (not failure or (not symptoms and _GUIDANCE_OBJECT.search(normalized)))
    ):
        return TicketContext("how_to", workflow, reason="guidance_requested")
    if failure and (feature or service):
        return TicketContext("unknown", reason="mixed_intents")
    if failure:
        category = "bug"
    elif service:
        return TicketContext("service_request", workflow, reason="individual_request")
    elif feature:
        category = "feature_request"
        symptoms = _matches(observed, _CAPABILITIES)
        if not symptoms and workflow in {
            "report_export",
            "contract_download",
            "attachments",
        }:
            symptoms = [
                f"format_{value}" for value in sorted(set(_FORMATS.findall(observed)))
            ]
    elif (
        resolved
        or _OTHER.search(normalized)
        or _GRATITUDE.fullmatch(normalized.strip())
    ):
        return TicketContext("other", "general", reason="informational")
    else:
        return TicketContext("unknown")

    if scope_reason:
        return TicketContext(category, workflow, reason=scope_reason)
    if len(symptoms) != 1:
        reason = "ambiguous_symptom" if symptoms else "missing_symptom"
        return TicketContext(category, workflow, reason=reason)
    operations = _operations(observed, workflow, category)
    if len(operations) > 1 or (workflow in _OPERATION_REQUIRED and not operations):
        return TicketContext(
            category,
            workflow,
            reason="ambiguous_operation" if operations else "missing_operation",
        )
    operation = operations[0] if operations else workflow
    return TicketContext(category, workflow, operation, symptoms[0], "context_resolved")
