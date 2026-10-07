"""Configurable, profession-neutral matching. No candidate-specific defaults."""
from dataclasses import dataclass, asdict
import re
from .model import Post

REJECTED = "вне предварительного отбора"


def has(pattern, text):
    return bool(re.search(pattern, text, re.I))


def normalized(text):
    return re.sub(r"[^\w]+", " ", text.casefold(), flags=re.UNICODE).strip()


def contains(text, phrase):
    # Literal whole-word phrases, with punctuation/space normalization. No remote regex.
    return (' '+normalized(phrase)+' ') in (' '+normalized(text)+' ')


@dataclass(frozen=True)
class Rules:
    target_roles: tuple[str, ...] = ()
    excluded_roles: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    required_keywords: tuple[str, ...] = ()
    excluded_keywords: tuple[str, ...] = ()
    allow_keyword_only: bool = False

    @classmethod
    def from_config(cls, config):
        data = config.get('matching', {})
        if not isinstance(data, dict): raise ValueError('matching must be a table')
        allowed = set(cls.__dataclass_fields__)
        if set(data)-allowed: raise ValueError('Unknown matching setting')
        values = {}
        for name in allowed-{'allow_keyword_only'}:
            items = data.get(name, [])
            if not isinstance(items,list) or any(not isinstance(x,str) or not normalized(x) for x in items):
                raise ValueError('Matching phrases must be non-empty strings')
            values[name] = tuple(items)
        flag=data.get('allow_keyword_only',False)
        if type(flag) is not bool: raise ValueError('allow_keyword_only must be boolean')
        rules=cls(**values,allow_keyword_only=flag)
        if not rules.target_roles: raise ValueError('Configure matching.target_roles before running')
        if flag and not rules.keywords: raise ValueError('Keyword-only matching needs keywords')
        return rules

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class Assessment:
    category: str
    reasons: tuple[str, ...]
    gaps: tuple[str, ...]
    fields: dict[str,str]
    role_kind: str = 'unknown'
    duties: tuple[str,...] = ()


def primary_role(text, rules=None):
    rules=rules or Rules()
    candidates=[]
    for line in [s.strip() for s in text.splitlines() if s.strip()][:18]:
        clean=re.sub(r'https?://\S+|#\S+','',line).strip(' •—-:;🚀📌💼')
        if not clean: continue
        if has(r'^(?:требования|обязанности|задачи|requirements|responsibilities|duties|what you will do)\b',clean): break
        if has(r'^(?:компания|company|локация|location|формат|salary|зарплата)\s*:',clean): continue
        if has(r'взаимодейств|collaborat|work with|team of',clean): continue
        clean=re.sub(r'^(?:вакансия|позиция|position|vacancy|job title)\s*[:—-]\s*','',clean,flags=re.I)
        candidates.append(clean)
        if any(contains(clean,x) for x in rules.excluded_roles): return clean,'other'
        if any(contains(clean,x) for x in rules.target_roles): return clean,'target'
    # Metadata without a policy uses the first meaningful line, not a profession dictionary.
    return (candidates[0] if candidates else 'Vacancy'), 'unknown'


def assess(post: Post, rules=None):
    rules=rules or Rules()
    title,kind=primary_role(post.text,rules)
    fields={'Заголовок из поста':title}
    if not rules.target_roles or kind=='other':
        return Assessment(REJECTED,(),(),fields,kind)
    if any(contains(post.text,x) for x in rules.excluded_keywords):
        return Assessment(REJECTED,(),(),fields,'other')
    if any(not contains(post.text,x) for x in rules.required_keywords):
        return Assessment(REJECTED,(),(),fields,'other')
    hits=tuple(x for x in rules.keywords if contains(post.text,x))
    if kind=='target':
        role=next(x for x in rules.target_roles if contains(title,x))
        return Assessment('совпадение с профилем',('Роль: '+role,)+hits[:2],(),fields,'target')
    if rules.allow_keyword_only and hits:
        return Assessment('нужно уточнить',hits[:3],(),fields,'candidate')
    return Assessment(REJECTED,(),(),fields,kind)


def refine(post, assessment, result):
    if assessment.category==REJECTED: return assessment
    # An explicit configured title survives model disagreement; ambiguous matches do not.
    if assessment.role_kind!='target' and result.get('verdict')!='match':
        return Assessment(REJECTED,(),(),assessment.fields,assessment.role_kind)
    duties=tuple(d for d in result.get('duties',[])[:2]
                 if isinstance(d,str) and 0<len(d)<=160 and d in post.text
                 and d!=assessment.fields['Заголовок из поста'])
    return Assessment(assessment.category,assessment.reasons,assessment.gaps,
                      assessment.fields,assessment.role_kind,duties)


def clipped(text, limit):
    text = "".join(c for c in text if c in "\n\t" or ord(c) >= 32)
    encoded = text.encode("utf-16-le")
    return text if len(encoded) <= limit * 2 else encoded[:(limit - 1) * 2].decode("utf-16-le", errors="ignore") + "…"


def render(post: Post, assessment: Assessment, discovered_at: str) -> str:
    # All facts are source excerpts. No unknown-field wall or speculative skill gaps.
    lines=[line.strip() for line in post.text.splitlines() if line.strip()]
    title=assessment.fields['Заголовок из поста']
    title=re.sub(r'^(?:позиция|вакансия|position|vacancy)\s*[:—-]\s*','',title,flags=re.I)
    title_parts=title.split('|')
    title=title_parts[0].strip()
    company=next((re.sub(r'^.*?(?:компания|company)\s*[:—-]\s*','',s,flags=re.I)
                  for s in lines if has(r'(?:компания|company)\s*[:—-]',s)), '')
    if not company and ' в ' in title:
        title,company=title.rsplit(' в ',1)
    if not company and len(title_parts)==3:
        company=title_parts[2].strip()
    chunks=['💼 '+clipped(title,110)]
    if company: chunks.append('🏢 '+clipped(company,75))
    def excerpts(pattern,limit=2):
        found=[]
        for line in lines:
            if not has(pattern,line): continue
            line=re.sub(r'https?://\S+|#\S+','',line).strip(' •—-;')
            if line and line not in found: found.append(line)
        return ' · '.join(found[:limit])
    location=excerpts(r'формат\w*\s*(?:работы)?\s*[:—-]|локаци\w*\s*[:—-]|location\s*:|'
                      r'work (?:mode|format)\s*:|remote|удал[её]н|релокац|relocat|\bhybrid\b|гибрид',1)
    if location:
        if '|' in location: location=location.split('|')[1].strip()
        chunks.append('🌍 '+clipped(location,155))
    salary=excerpts(r'(?:зарплат|salary|вилка|з/п|compensation)\s*[:—-]|(?:\d[\d .,kк]*\s*(?:USD|EUR|RUB|₽|€|\$|руб))|(?:[$€]\s*\d)',1)
    if salary: chunks.append('💰 '+clipped(salary,110))
    reasons=[r for r in assessment.reasons if r!='Разбор обязанностей'][:3]
    if reasons: chunks += ['', '🎯 '+clipped(' · '.join(reasons),125)]
    if assessment.duties: chunks.append('📝 '+clipped('; '.join(assessment.duties),230))
    conditions=excerpts(r'\bEnglish\b|английск|residents? only|outside Russia|только (?:РФ|ЕС|EU)|'
                        r'work permit|гражданств|on.call|дежурств|ночн|night shift',2)
    if conditions: chunks.append('⚠️ '+clipped(conditions,160))
    if any('Подозрительные' in g for g in assessment.gaps):
        chunks.append('⚠️ В посте есть подозрительные условия оплаты — проверьте оригинал.')
    chunks += ['', '🔗 '+clipped(post.original_url,500)]
    result = "\n".join(chunks)
    # Telegram limit is 4096; conservatively count UTF-16 units, including emoji.
    if len(result.encode("utf-16-le")) // 2 > 4096:
        raise ValueError("Message too long; source needs manual review")
    return result
