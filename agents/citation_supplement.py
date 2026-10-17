"""引用核验与补全(纯标准库,不联网)。

引用 key 只认本地文献库(inbox/references/refs.bib):
- citable_keys():合法 key 的唯一来源;
- apply_insertions():把评审标出的“缺引用句”机械地补上 \\cite{key},key 不在
  白名单里的一律留给人,绝不插入模型自报的 key;
- unknown_cite_keys():扫描正文里所有 \\cite(含逗号分隔多 key)中的未知 key。
"""
import re
from pathlib import Path

_CITE_KEYS_RE = re.compile(r"\\(?:paren|text|auto|foot|smart|super)?cite[a-zA-Z]*\*?(?:\[[^\]]*\])*\{([^}]*)\}")


def citable_keys() -> set:
    """合法引用 key = refs.bib 里的全部 key。"""
    import config
    from .references import citable_keys as _bib_keys
    return _bib_keys(config.REFS_BIB_PATH)


def unknown_cite_keys(text: str, allowed: set) -> list[str]:
    """正文里所有 \\cite{...}(含逗号分隔多 key)中不在 allowed 里的 key。"""
    bad = []
    for m in _CITE_KEYS_RE.finditer(text or ""):
        for key in (k.strip() for k in m.group(1).split(",")):
            if key and key != "*" and key not in allowed and key not in bad:
                bad.append(key)
    return bad


def apply_insertions(text: str, insertions: list[dict], allowed_keys: set | None = None) -> tuple:
    """在每个被标记句子末尾插入 ``\\cite{key}``。

    insertions: [{"sentence", "suggested_key_or_url", "source"}];只有 source == "bib"
    且 key 在 allowed_keys 里才会插入(allowed_keys 为 None 时不校验,仅供单元测试)。
    返回 (新文本, 插入数, 跳过清单)。句子在正文里找不到(已被改过)也进跳过清单。
    """
    new_text = text
    applied = 0
    skipped = []
    for ins in insertions:
        if ins.get("source") != "bib" or not ins.get("suggested_key_or_url"):
            skipped.append({**ins, "reason": "no local key; left to human"})
            continue
        sentence = ins.get("sentence", "")
        key = ins.get("suggested_key_or_url", "")
        if not sentence or not key:
            skipped.append({**ins, "reason": "missing sentence or key"})
            continue
        if allowed_keys is not None and key not in allowed_keys:
            skipped.append({**ins, "reason": f"key '{key}' not in local library; left to human"})
            continue
        if sentence in new_text:
            new_text = new_text.replace(sentence, sentence + f" \\cite{{{key}}}", 1)
            applied += 1
        else:
            skipped.append({**ins, "reason": "sentence not found verbatim in document"})
    return new_text, applied, skipped
