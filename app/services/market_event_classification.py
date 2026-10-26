"""Explainable keyword rules with deliberately bounded explicit negation."""
import hashlib
import json
import re

RULE_VERSION = "keyword-v2-explicit-negation"


def analyze_market_text(title, summary, scope_words, level_words, sentiment_words):
    text = f"{title} {summary or ''}"
    words = set(word for group in (scope_words,level_words,sentiment_words)
                for entries in group.values() for word in entries)
    terms = "|".join(re.escape(word) for word in sorted(words,key=lambda word:(-len(word),word)))
    spans = []
    if terms:
        # Adjacent, explicit constructions only. Do not infer that removing an
        # adverse event is automatically positive for every asset or sector.
        prefix = r"(?:取消|解除|撤销|终止|停止|未发生|未出现|没有发生|没有出现|不存在|并无|否认|不再(?:发生|出现|实施)?|无)"
        links = r"(?:了|的|相关|新的|任何|发生|存在|实施|采取){0,2}(?:对(?:中国|美国|俄罗斯|伊朗|欧盟|华|美|俄|伊|欧)的?)?"
        pattern = re.compile(prefix+links+r"(?:"+terms+r")(?:风险|事件|措施)?")
        for match in pattern.finditer(text):
            before = text[max(0,match.start()-5):match.start()]
            if not re.search(r"(?:并未|没有|尚未|未曾|无法|不能|未能|难以|不)$",before):
                spans.append(match.span())
        improvement = re.compile(r"(?:(?:系统性|违约|停牌|退市|暴跌)风险|风险)(?:明显|大幅|显著|持续)?(?:下降|降低|缓解|消退|解除)")
        spans.extend(match.span() for match in improvement.finditer(text))

    def hits(entries):
        return [word for word in entries if any(
            not any(start <= match.start() and match.end() <= end for start,end in spans)
            for match in re.finditer(re.escape(word),text))]

    scope_hits = {scope:hits(entries) for scope,entries in scope_words.items()}
    best_scope,score = "other",0
    for scope,matched in scope_hits.items():
        if len(matched)>score:
            best_scope,score=scope,len(matched)
    level_hits = {str(level):hits(entries) for level,entries in level_words.items()}
    level = next((level for level in range(5,1,-1) if level_hits.get(str(level))),2)
    positive,negative = hits(sentiment_words["positive"]),hits(sentiment_words["negative"])
    direction = "positive" if len(positive)>len(negative) else "negative" if len(negative)>len(positive) else "neutral"
    fingerprint = hashlib.sha256(json.dumps([scope_words,level_words,sentiment_words],
        ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:16]
    return dict(rule_version=RULE_VERSION,rule_fingerprint=fingerprint,current_scope=best_scope,
        current_level=level,current_sentiment=direction,scope_hits=scope_hits,level_hits=level_hits,
        positive_hits=positive,negative_hits=negative,
        negated_phrases=list(dict.fromkeys(text[start:end] for start,end in sorted(spans))),
        scope_ties=[scope for scope,matched in scope_hits.items() if score and len(matched)==score])
