"""Whether an email asks not to be emailed again (0.12.0), in the languages Ember's mail is likely to come in.

Only the sender's own words count: what they quote of an earlier email (lines starting with ">", and everything from a
header such as "-----Original Message-----" or "From: ..." on, which starts a quote without ">") and their signature
are left out, so Ember's own footer ('Reply "stop" ...') in a quoted reply never counts. 0.39.5: a quote's first line
("On ... wrote:", "Am ... schrieb ...:") ended them too, so a reply written below the quote (Thunderbird's default) or
into it was never read; now their words after a quote marked with ">" count. It asks when one of its first lines is
only a word like "stop", "unsubscribe" or "abmelden" (or a short line with "stop" and only words like "please", "no"
or "now", not a question), when the subject is one, or when one of its lines or its subject holds a phrase such as
"remove me", "don't email me", "keine E-Mails mehr", "désinscrire", "darme de baja", "non scrivetemi" or "afmelden".
0.15.0: its first lines are the first FIRST_LINES after a greeting (only the very first counted, so "Hello,\nPlease
stop." was missed), and more phrases count; 0.39.5: the first lines of each part of their own words, the part below
a quote too, and (0.39.7) below a quote of any length. A false alarm only means Ember doesn't write to that sender
again (they can write first); a miss would break the law (GDPR Art. 21, UWG § 7), so the phrases lean towards asking.
The agent marks what this misses (mark_opt_out), and the owner can add any address. 0.40.3 (analysis-0.40.1 codebase,
M1): more phrasings ("mich nicht mehr zu kontaktieren", "unterlassen Sie ... Kontaktaufnahme", "sehen Sie von weiteren
E-Mails ab", "do not reach out to me again", "please remove"), a mail gateway's tag before the subject ("[EXTERNAL]")
is left out, and a line's link or "[" hides only the phrase in its own sentence (_INSTRUCTION); and an approved email
waits until the agent has opened its recipient's newer email (executor.REPLIED), so what this misses is read first.
"""

from __future__ import annotations

import bisect
import re
import unicodedata

OWN_CHARS = 2_000  # of the sender's own words: an opt-out comes first
# 0.39.5: of the text read for them. 0.39.7: what they quote (">") doesn't count, however long it is: counted with
# the quotes, these 8,000 characters missed a stop written below a thread quoted three rounds deep
READ_CHARS = 8_000
REASON_CHARS = 60
FIRST_LINES = 3  # 0.15.0: the own lines, after a greeting, in which a short "stop" asks
SHORT_WORDS = 4
# A quote's first line. 0.39.5: the sender's own words may follow its lines (">"): below it, or between its parts.
_ATTRIBUTION = re.compile(
    r"^(?:on|am|le|el|il|op|em|w dniu)\b.{0,300}\b(?:wrote|schrieb|a ecrit|escribio|ha scritto|schreef|escreveu"
    r"|napisal)\b.{0,100}:$"
)
# Where the sender's own words end: an email quoted or forwarded without ">", and their signature.
_OWN_END = re.compile(
    r"^(?:"
    r"-{2,} ?(?:original message|urspr[a-z]* nachricht|message d.origine|mensaje original|messaggio originale"
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
    # 0.40.3 (analysis-0.40.1 codebase, M1): "do not reach out to me again", "I'd prefer not to receive any further
    # emails", "please remove us", "not interested, please remove", "cease all contact"
    r"(?:(?:do not|don'?t|dont|never) reach|(?:please|kindly) stop reaching) out(?: to (?:me|us))?"
    r"(?: (?:again|any ?more)\b|(?: in (?:the )?future)?(?= *(?:[.!,;]|$)))|"
    r"(?:(?:prefer|like|want|wish) not to|rather not) (?:receive|get) "
    r"(?:any (?:further |more )?|further |more |these |such |your )(?:e-?mails?|mails?|messages?|newsletters?"
    r"|communications?)|"
    r"please remove(?: (?:me|us)\b|(?= *(?:[.!,;]|$)))|(?:remove|take) us(?: off)?(?= *(?:[.!,;]|$))|"
    r"cease (?:all |any )?(?:further )?(?:contact|communications?|e-?mails?|e-?mailing|messages?)\b|"
    # German
    r"abmelden|abmeldung|abbestell\w*|austragen|"
    r"keine (?:weiteren |weitere )?(?:e-?mails?|mails?|nachrichten|werbung|newsletter|kontaktaufnahmen?"
    r"|zusendungen?)(?: mehr)?\b|"
    # 0.40.3 (analysis-0.40.1 codebase, M1): "keine weitere Kontaktaufnahme" (above), "keinen weiteren Kontakt"
    r"(?:keine (?:weitere )?kommunikation|keinen (?:weiteren kontakt|kontakt mehr))(?: mehr)?(?= *(?:[.!,;]|$))|"
    # 0.40.3 (analysis-0.40.1 codebase, M1): with "zu" ("mich nicht mehr zu kontaktieren", "mir nicht mehr zu
    # schreiben", "nicht mehr anzuschreiben")
    r"nicht mehr (?:zu )?(?:an(?:zu)?)?(?:schreiben|kontaktieren|mailen)|"
    # 0.40.3: "unterlassen Sie künftig jede weitere Kontaktaufnahme", "weitere E-Mails zu unterlassen", "sehen Sie
    # von weiteren E-Mails ab", "von weiteren Kontaktaufnahmen abzusehen", "sehen Sie davon ab, mir zu schreiben"
    r"unterlassen sie\b[^.!?;]{0,40}?\b(?:kontakt\w*|e-?mails?|mails?|nachrichten|zusendung\w*|werbung)|"
    r"(?:kontakt\w*|e-?mails?|mails?|nachrichten|zusendung\w*) (?:zu )?unterlassen\b|"
    r"sehen sie (?:bitte )?(?:kunftig |zukunftig |in zukunft )?von (?:weiteren |jeglichen |kunftigen "
    r"|jeder (?:weiteren )?)?(?:e-?mails?|mails?|nachrichten|kontakt\w*|zusendung\w*|anschreiben|werbung)"
    r"(?: an (?:mich|uns))? ab(?= *(?:[.!,;]|$))|"
    r"von (?:weiteren |jeglichen |kunftigen |jeder (?:weiteren )?)?(?:e-?mails?|mails?|nachrichten|kontakt\w*"
    r"|zusendung\w*|anschreiben|werbung)(?: an (?:mich|uns))? abzusehen\b|"
    r"sehen sie (?:bitte )?davon ab,? (?:mir|mich|uns) (?:weiter(?:hin)? |noch )?(?:zu (?:schreiben|kontaktieren"
    r"|mailen)|anzuschreiben|anzumailen)\b|"
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
# 0.40.3 (analysis-0.40.1 codebase, M1): the tags a mail gateway puts before a subject ("[EXTERNAL] STOP", "[Ext]",
# "EXTERN: Stopp", "***SPAM***"), and the prefixes after them; a subject that is only a tag ("[STOP]") keeps it
_SUBJECT_TAGS = re.compile(
    r"^(?:(?:\[[^\[\]]{1,40}\]|\*{1,3}[^*]{1,30}\*{1,3}|(?:extern\w*|ext|caution|warnung|achtung|spam)\s*:)\s*"
    r"(?:(?:re|aw|wg|fwd?|antw|sv|rv|r|tr)\s*:\s*)*)+"
)
# 0.40.3: where a sentence ends within a line (_INSTRUCTION judges the sentence a phrase is in, not its whole line)
_SENTENCE_END = re.compile(r"[.!?;](?=\s|$)")
_PUNCTUATION = re.compile(r"[^\w' -]")


def _plain(text: str) -> str:
    """Lower case, without accents, typographic apostrophes as plain ones, single spaces."""
    text = text.replace("’", "'").replace("‘", "'").replace("ß", "ss")
    folded = "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))
    return " ".join(folded.split())


def _bare(plain: str) -> str:
    return " ".join(_PUNCTUATION.sub("", plain).split())


def own_parts(body: str) -> list[list[tuple[str, str]]]:
    """The sender's own lines (each as written and plain), without what they quote and their signature, in parts: the
    lines above a quote, and (0.39.5) those after a quote marked with ">", a reply written below the quote or into
    it, each a part of its own. Lines that follow a quote's first line without ">" are a quote its writer's program
    didn't mark, and end them. 0.39.7: what they quote doesn't count toward the READ_CHARS read."""
    parts: list[list[tuple[str, str]]] = [[]]
    size = read = 0
    cited = False
    for line in body.splitlines():
        plain = _plain(line)
        if not plain:
            continue
        quoted = plain.startswith(">")
        read += 0 if quoted else len(plain)
        if read > READ_CHARS or (not quoted and (cited or _OWN_END.match(plain))):
            break
        if quoted or _ATTRIBUTION.match(plain):
            cited = not quoted  # a quote marked as one: what follows it is the sender's again
            if parts[-1]:
                parts.append([])
            continue
        parts[-1].append((line.strip(), plain))
        size += len(plain)
        if size >= OWN_CHARS:
            break
    return [part for part in parts if part]


def _asks_alone(plain: str) -> bool:
    """Whether a line is only a word that asks ("Stop.", "Unsubscribe"), or a short one with "stop" that asks (0.15.0:
    one with other words than _WITH_STOP, or a question, doesn't)."""
    bare = _bare(plain)
    words = set(bare.split())
    if bare in _ALONE:
        return True
    short = 0 < len(bare.split()) <= SHORT_WORDS and not plain.rstrip().endswith("?")
    return short and not _STOP.isdisjoint(words) and words <= _STOP | _WITH_STOP


def _topic(subject: str | None) -> str:
    """A subject, plain, without its prefixes ("Re:", "AW:") and (0.40.3) a mail gateway's tags before them."""
    topic = _SUBJECT_PREFIX.sub("", _plain(subject or ""))
    untagged = _SUBJECT_TAGS.sub("", topic)
    return untagged if _bare(untagged) else topic


def _asks(plain: str) -> bool:
    """Whether a line holds a phrase that asks, in a sentence that doesn't tell the reader how to unsubscribe
    (_INSTRUCTION). 0.40.3 (analysis-0.40.1 codebase, M1): judged on the phrase's sentence: on the whole line, "Please
    don't email me again. Our policy: https://..." read as a newsletter's footer, and the stop was missed."""
    ends = [0, *(m.end() for m in _SENTENCE_END.finditer(plain)), len(plain)]
    for match in _PHRASES.finditer(plain):
        first, last = bisect.bisect_right(ends, match.start()) - 1, bisect.bisect_left(ends, match.end())
        if not _INSTRUCTION.search(plain[ends[first] : ends[last]]):  # the sentence (or two) the phrase is in
            return True
    return False


def opt_out(subject: str | None, body: str | None) -> str | None:
    """The words in which an email asks not to be emailed again (its line that does, or its subject), or None."""
    parts = own_parts(body or "")
    topic = _topic(subject)
    for part in parts:  # 0.39.5: each part's, a reply's below the quote too (only the email's first lines counted)
        counted = 0
        for written, plain in part:  # the first lines, a greeting aside (0.15.0: only the first line counted)
            if _asks_alone(plain):
                return written[:REASON_CHARS]
            if len(plain.split()) > SHORT_WORDS or not _GREETING.match(plain):
                counted += 1
            if counted >= FIRST_LINES:
                break
    lines = [line for part in parts for line in part]
    if _bare(topic) in _ALONE:
        return (subject or "").strip()[:REASON_CHARS]
    for written, plain in lines:
        if _asks(plain):
            return written[:REASON_CHARS]
    return (subject or "").strip()[:REASON_CHARS] if _PHRASES.search(topic) else None
