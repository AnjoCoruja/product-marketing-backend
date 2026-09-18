"""Parser determinístico das respostas do usuário no Telegram.

Sem LLM aqui: respostas como "M, 35, 59,90" ou "tamanho M, atacado 35"
são interpretadas por regras. Isso torna a atualização de campos
testável, auditável e imune a alucinação.

Estratégia:
1. Divide a mensagem em tokens (vírgula, ponto-e-vírgula ou quebra de linha).
2. Tokens rotulados ("tamanho M", "atacado: 35", "varejo 59,90") têm
   prioridade e casam com o campo pelo rótulo.
3. Tokens sem rótulo são classificados: número => preço; texto => size.
4. Preços sem rótulo preenchem os campos de preço pendentes na ordem
   (wholesale_price antes de retail_price).
5. O que não for interpretável vai para `unparsed`, e campos cujo token
   não pôde ser convertido (ex.: preço "abc") viram erros.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

FIELD_LABELS_PT = {
    "name": "nome",
    "description": "descrição",
    "color": "cor",
    "size": "tamanho",
    "wholesale_price": "preço de atacado",
    "retail_price": "preço de varejo",
}

_LABEL_ALIASES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(nome|produto)\b", re.I), "name"),
    (re.compile(r"^(descri[cç][aã]o|desc)\b", re.I), "description"),
    (re.compile(r"^cor\b", re.I), "color"),
    (re.compile(r"^(tamanho|tam)\b", re.I), "size"),
    (re.compile(r"^(atacado|custo|pre[cç]o\s+de\s+atacado)\b", re.I), "wholesale_price"),
    (re.compile(r"^(varejo|venda|pre[cç]o\s+de\s+(varejo|venda))\b", re.I), "retail_price"),
]

_PRICE_RE = re.compile(r"^[R$€\s]*(\d{1,3}(?:\.\d{3})*|\d+)([.,]\d{1,2})?$")


@dataclass
class ParsedUserResponse:
    updates: dict[str, object] = field(default_factory=dict)
    unparsed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def parse_price(raw: str) -> float | None:
    """Converte "35", "35,90", "R$ 1.299,90" em float. None se inválido."""
    text = raw.strip()
    m = _PRICE_RE.match(text)
    if not m:
        return None
    integer = m.group(1).replace(".", "")
    decimals = (m.group(2) or "").replace(",", ".").lstrip(".")
    try:
        return float(f"{integer}.{decimals}" if decimals else integer)
    except ValueError:
        return None


def _match_label(token: str) -> tuple[str | None, str]:
    """Retorna (campo, resto do token) se o token começa com um rótulo."""
    for pattern, field_name in _LABEL_ALIASES:
        m = pattern.match(token.strip())
        if m:
            rest = token.strip()[m.end():].lstrip(" :,-=")
            return field_name, rest
    return None, token


def _smart_split(message: str) -> list[str]:
    """Divide a resposta em tokens.

    Decimais brasileiros usam vírgula ("59,90"), então uma vírgula só
    é separador quando NÃO está entre dígitos formando 2 casas decimais.
    """
    sep = chr(0)
    protected = re.sub(r"(\d),(\d{1,2})(?!\d)", lambda m: m.group(1) + sep + m.group(2), message)
    tokens = [t.replace(sep, ",").strip() for t in re.split(r"[,;\n]+", protected)]
    return [t for t in tokens if t]


def parse_user_response(message: str, pending_fields: list[str]) -> ParsedUserResponse:
    result = ParsedUserResponse()
    tokens = _smart_split(message)
    pending = list(pending_fields)

    # Passada 1: tokens rotulados
    unlabeled: list[str] = []
    for token in tokens:
        field_name, rest = _match_label(token)
        if field_name and field_name in pending:
            _assign(result, field_name, rest)
            if field_name in pending:
                pending.remove(field_name)
        elif field_name:
            result.unparsed.append(token)
        else:
            unlabeled.append(token)

    # Passada 2: tokens sem rótulo, na ordem dos campos pendentes
    for token in unlabeled:
        price = parse_price(token)
        if price is not None:
            target = next(
                (f for f in pending if f in ("wholesale_price", "retail_price")),
                None,
            )
            if target:
                result.updates[target] = price
                pending.remove(target)
            else:
                result.unparsed.append(token)
            continue

        target = next(
            (f for f in pending if f in ("size", "color", "name", "description")),
            None,
        )
        if target:
            result.updates[target] = token
            pending.remove(target)
        else:
            result.unparsed.append(token)

    return result


def _assign(result: ParsedUserResponse, field_name: str, raw_value: str) -> None:
    if field_name in ("wholesale_price", "retail_price"):
        price = parse_price(raw_value)
        if price is None:
            result.errors.append(
                f"Não entendi o {FIELD_LABELS_PT[field_name]}: '{raw_value}'. "
                "Envie apenas números, ex.: 35 ou 59,90."
            )
        else:
            result.updates[field_name] = price
    else:
        if raw_value:
            result.updates[field_name] = raw_value
        else:
            result.errors.append(f"Campo '{FIELD_LABELS_PT[field_name]}' veio vazio.")


def build_missing_fields_question(missing_fields: list[str]) -> str:
    labels = [FIELD_LABELS_PT.get(f, f) for f in missing_fields]
    if not labels:
        return ""
    if len(labels) == 1:
        lista = labels[0]
    else:
        lista = ", ".join(labels[:-1]) + " e " + labels[-1]
    return f"Preciso de: {lista}. Pode responder na ordem, separado por vírgula."
