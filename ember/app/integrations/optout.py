"""Whether an email asks not to be emailed again (0.12.0), in the languages Ember's mail is likely to come in.

Only the sender's own words count: what they quote of an earlier email (lines starting with ">", everything from a reply
header such as "On ... wrote:" or "-----Original Message-----" on) and their signature are left out, so Ember's own
footer ('Reply "stop" ...') in a quoted reply never counts. It asks when one of its first lines is only a word like
"stop", "unsubscribe" or "abmelden" (or a short line with "stop" and only words like "please", "no" or "now", not a
question), when the subject is one, or when one of its lines or its subject holds a phrase such as "remove me", "don't
email me", "keine E-Mails mehr", "désinscrire", "darme de baja", "non scrivetemi" or "afmelden". 0.15.0: its first lines
are the first FIRST_LINES after a greeting (only the very first counted, so "Hello,\nPlease stop." was missed), and more
phrases count. A false alarm only means Ember doesn't write to that sender again (they can write first); a miss would
break the law (GDPR Art. 21, UWG § 7), so the phrases lean towards asking. The agent marks what this misses
(mark_opt_out), and the owner can add any address.
"""

from __future__ import annotations

import re
import unicodedata

OWN_CHARS = 2_000  # of the text: an opt-out comes first
REASON_CHARS = 60
FIRST_LINES = 3  # 0.15.0: the own lines, after a greeting, in which a short "stop" asks
SHORT_WORDS = 4
_REPLY_START = re.compile(
    r"^(?:"
    r"(?:on|am|le|el|il|op|em|w dniu)\b.{0,300}\b(?:wrote|schrieb|a ecrit|escribio|ha scritto|schreef|escreveu"
    r"|napisal)\b.{0,100}:"
    r"|-{2,} ?(?:original message|urspr[a-z]* nachricht|message d.origine|mensaje original|messaggio originale"
    r"|oorspronkelijk bericht|mensagem original|wiadomosc oryginalna) ?-{2,}"
    r"|(?:from|von|de|da|van|od|sent|gesendet|envoye|enviado|inviato|verzonden) ?:.*"
    r"|-- ?|_{5,}"
    r")$"
)
# A first line (or subject) that is only one of these asks to stop.
_ALONE = frozenset(
    {
        "stop",
        "stopp",
        "stop please",
        "please stop",
        "bitte stopp",
        "bitte stop",
        "stopp bitte",
        "stop bitte",
        "unsubscribe",
        "unsubscribe me",
        "please unsubscribe",
        "remove",
        "remove me",
        "opt out",
        "optout",
        "abmelden",
        "abbestellen",
        "austragen",
        "desinscrire",
        "desabonner",
        "desinscription",
        "baja",
        "darme de baja",
        "afmelden",
        "uitschrijven",
        "cancellami",
        "disiscrivimi",
        "descadastrar",
        "wypisz",
        "arret",
        "arrete",
        "basta",
    }
)
_PHRASES = re.compile(
    r"\b(?:"
    # English
    r"unsubscrib\w*|opt(?:ing)?[ -]?out|remove me|take me off|"
    r"(?:do not|don'?t|dont|never) (?:e-?mail|mail|contact|write(?: to)?|message|send) (?:me\b|us(?= *(?:[.!,;]|$)))|"
    # 0.15.0
    r"(?:please|kindly) stop(?: (?:it|this|that|now|(?:e-?mailing|mailing|contacting|writing|messaging|spamming)"
    r"(?: (?:me|us))?))?(?= *(?:[.!,;]|$))|stop(?: it)?(?: please)?[.!,;]? (?:i'?m|we'?re|i am|we are) not interested|"
    r"leave me alone(?= *(?:[.!,;]|$))|"
    r"remove my e-?mail(?: address)?(?= *(?:[.!,;]|$))|"
    r"(?:(?:i|we)(?:'?d| would) like|(?:i|we) want|(?:i|we) ask) (?:you|ember) to stop"
    r"(?= *(?:[.!,;]|$| (?:e-?mailing|mailing|writing|contacting|messaging|sending)))|"
    r"(?:remove|take) (?:me|us|my (?:e-?mail )?address) (?:from|off) "
    r"(?:your |the |this |all )?(?:(?:e-?)?mail(?:ing)? )?"
    r"(?:lists?|database|records|newsletters?|contacts|distribution)|"
    r"take my (?:e-?mail(?: address)?|address|name) off\b|"
    r"(?:do not|don'?t|dont|never) (?:e-?mail|mail|write|contact|message)(?: (?:to )?(?:me|us))? (?:again|any ?more)\b|"
    r"(?:do not|don'?t|dont|no longer) (?:want|wish)(?: to (?:receive|get))? (?:any )?"
    r"(?:more |further |these |those |your |such )?(?:e-?mails?|mails?|messages?|newsletters?|communications?)"
    r"(?: from (?:you|ember|your \w+))?(?= *(?:[.!,;]|$))|"
    r"stop (?:e-?mailing|mailing|contacting|writing to|messaging|spamming) (?:me|us)\b|stop spamming\b|"
    r"stop sending (?:me )?(?:(?:any|these|those|your|more|the|such) )?(?:e-?mails?|mails?|messages?|newsletters?)|"
    r"stop sending(?: me)?(?: (?:these|those|this|them|that|it))?(?= *(?:[.!,;]|$))|"
    r"no (?:more|further) (?:e-?mails?|mails?|messages?|contact)|"
    r"object to (?:the )?(?:processing|use) of my|"
    # German
    r"abmelden|abmeldung|abbestell\w*|austragen|"
    r"keine (?:weiteren |weitere )?(?:e-?mails?|mails?|nachrichten|werbung|newsletter)(?: mehr)?\b|"
    r"nicht mehr (?:an)?(?:schreiben|kontaktieren|mailen|anmailen)|"
    r"(?:schreiben|mailen|kontaktieren) sie mich (?:bitte )?nicht|"
    r"(?:schreiben sie|mailen sie|schreib|schreibt|mail|mailt) mir (?:bitte )?nicht mehr|"
    r"(?:melden sie|meldet) mich (?:bitte )?(?:hier |davon |dort )?ab\b|"
    # 0.15.0
    r"nicht mehr (?:an)?ge(?:schrieben|mailt)|nicht mehr (?:angemailt|kontaktiert)|"
    r"(?:entfernen|streichen|loschen|austragen) sie mich(?! nicht\b)|nehmen sie mich (?:bitte )?(?:aus|von)\b|"
    r"stoppen sie|"
    r"horen sie (?:bitte )?(?:damit )?auf(?:,? (?:mir |mich )?zu (?:schreiben|mailen|kontaktieren))?(?= *(?:[.!,;]|$))|"
    r"hoe?rt? (?:bitte )?(?:damit )?auf(?: damit)?(?:,? (?:mir |mich )?zu (?:schreiben|mailen|kontaktieren))?"
    r"(?= *(?:[.!,;]|$))|"
    r"lass(?:en sie|t)? mich (?:bitte )?(?:endlich )?in ruhe(?= *(?:[.!,;]|$))|"
    r"widersprech\w*|widerspreche|widerspruch|"
    r"loschen sie meine (?:daten|e-?mail|adresse)|"
    # French
    r"desinscri\w*|desabonn\w*|ne (?:plus |pas )?me (?:contacter|ecrire|envoyer)|"
    r"ne m'?(?:ecrivez|contactez|envoyez) plus|"
    # Spanish
    r"(?:dar|darme|darse) de baja|cancelar (?:la |mi )?suscripcion|"
    r"no (?:me )?(?:escriba|escribas|contacte|contactes|envie|envies)\b|no quiero recibir|"
    # Italian
    r"cancellami|disiscriv\w*|non (?:mi )?(?:scrive(?:te|re|rmi|temi)|contatta(?:te|re|rmi|temi)|inviate(?:mi)?)\b|"
    r"non voglio (?:piu )?ricevere|"
    # Dutch
    r"afmelden|uitschrijven|geen (?:e-?mails?|mails?|berichten) meer|"
    # Portuguese
    r"descadastr\w*|cancelar (?:a |minha )?inscricao|nao quero (?:mais )?receber|"
    # Polish
    r"wypis[az]\w*|rezygnuj\w*"
    r")"
)
_STOP = frozenset({"stop", "stopp", "stoppen", "aufhoren"})
# 0.15.0: the only other words a short line with "stop" may have to ask ("No, stop.", "Stop it now please"): not
# "don't stop", "stop by", "full stop", "stop! I love it"
_WITH_STOP = frozenset(
    {"please", "pls", "plz", "bitte", "no", "nein", "thanks", "thank", "you", "danke", "now", "jetzt", "sofort", "just",
     "ok", "okay", "already", "schon", "more", "mehr", "it", "this", "that", "das", "damit", "immediately", "again",
     "and", "und", "but", "aber", "so", "i", "said", "enough", "genug", "right", "away", "endlich", "finally",
     "einfach", "really", "seriously", "all", "alle", "the", "these", "diese", "die", "spam", "e-mails", "emails",
     "mails", "e-mail", "email", "mail", "emailing", "e-mailing", "mailing", "writing", "contacting", "messaging",
     "sending", "spamming", "ember", "hey", "kindly", "me", "us"}
)  # fmt: skip
# 0.15.0: a line that only greets ("Hello Ember,", "Hallo,", "Sehr geehrte Damen und Herren,")
_GREETING = re.compile(
    r"^(?:hi|hello|hey|dear|hallo|moin|servus|liebe[rs]?|sehr geehrte[rs]?|guten (?:morgen|tag|abend)|"
    r"good (?:morning|afternoon|evening)|bonjour|salut|hola|ciao|buongiorno|hoi|beste|ola|dzien dobry|witam)\b"
)
# A line that tells the reader how to unsubscribe (a newsletter's footer, even without its list headers): not someone
# asking.
_INSTRUCTION = re.compile(
    r"https?://|www\.|\[|\bclick|\bklick|\bcliquez|\bclic\b|\bclicca|\bklik\b|\byou (?:are )?(?:receiv|get)\w*|"
    r"\bto unsubscribe|\bmanage (?:your )?(?:subscription|preferences|e-?mail)|\bsie erhalten|\bum sich\b|"
    r"\bpour vous desinscrire|\bpara (?:darte|darse|darme) de baja"
)
_SUBJECT_PREFIX = re.compile(r"^(?:(?:re|aw|wg|fwd?|antw|sv|rv|r|tr)\s*:\s*)+")
_PUNCTUATION = re.compile(r"[^\w' -]")


def _plain(text: str) -> str:
    """Lower case, without accents, typographic apostrophes as plain ones, single spaces."""
    text = text.replace("’", "'").replace("‘", "'").replace("ß", "ss")
    folded = "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))
    return " ".join(folded.split())


def _bare(plain: str) -> str:
    return " ".join(_PUNCTUATION.sub("", plain).split())


def own_lines(body: str) -> list[tuple[str, str]]:
    """The sender's own lines (each as written and plain), before what they quote and their signature."""
    lines = []
    for line in body[:OWN_CHARS].splitlines():
        plain = _plain(line)
        if _REPLY_START.match(plain):
            break
        if plain and not plain.startswith(">"):
            lines.append((line.strip(), plain))
    return lines


def _asks_alone(plain: str) -> bool:
    """Whether a line is only a word that asks ("Stop.", "Unsubscribe"), or a short one with "stop" that asks (0.15.0:
    one with other words than _WITH_STOP, or a question, doesn't)."""
    bare = _bare(plain)
    words = set(bare.split())
    if bare in _ALONE:
        return True
    short = 0 < len(bare.split()) <= SHORT_WORDS and not plain.rstrip().endswith("?")
    return short and not _STOP.isdisjoint(words) and words <= _STOP | _WITH_STOP


def opt_out(subject: str | None, body: str | None) -> str | None:
    """The words in which an email asks not to be emailed again (its line that does, or its subject), or None."""
    lines = own_lines(body or "")
    topic = _SUBJECT_PREFIX.sub("", _plain(subject or ""))
    counted = 0
    for written, plain in lines:  # the first lines, a greeting aside (0.15.0: only the first line counted)
        if _asks_alone(plain):
            return written[:REASON_CHARS]
        if len(plain.split()) > SHORT_WORDS or not _GREETING.match(plain):
            counted += 1
        if counted >= FIRST_LINES:
            break
    if _bare(topic) in _ALONE:
        return (subject or "").strip()[:REASON_CHARS]
    for written, plain in lines:
        if _PHRASES.search(plain) and not _INSTRUCTION.search(plain):
            return written[:REASON_CHARS]
    return (subject or "").strip()[:REASON_CHARS] if _PHRASES.search(topic) else None
