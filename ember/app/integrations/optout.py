"""Whether an email asks not to be emailed again (0.12.0), in the languages Ember's mail is likely to come in.

Only the sender's own words count: what they quote of an earlier email (lines starting with ">", everything from a
reply header such as "On ... wrote:" or "-----Original Message-----" on) and their signature are left out, so Ember's
own footer ('Reply "stop" ...') in a quoted reply never counts. It asks when its first line is only a word like
"stop", "unsubscribe" or "abmelden" (or a short line with "stop" that doesn't say "don't stop"), when the subject is
one, or when one of its lines or its subject holds a phrase such as "remove me", "don't email me", "keine E-Mails
mehr", "désinscrire", "darme de baja", "non scrivetemi" or "afmelden". A false alarm only means Ember doesn't write
to that sender again (they can write first); a miss would break the law (GDPR Art. 21, UWG § 7), so the phrases lean
towards asking. The agent marks what this misses (mark_opt_out), and the owner can add any address.
"""

from __future__ import annotations

import re
import unicodedata

OWN_CHARS = 2_000  # of the text: an opt-out comes first
REASON_CHARS = 60
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
    r"(?:do not|don'?t|dont|never) (?:e-?mail|mail|contact|write(?: to)?|message|send) me\b|"
    r"stop (?:e-?mailing|mailing|contacting|writing to|messaging) me|"
    r"stop sending (?:me )?(?:(?:any|these|those|your|more|the|such) )?(?:e-?mails?|mails?|messages?|newsletters?)|"
    r"no (?:more|further) (?:e-?mails?|mails?|messages?|contact)|"
    r"object to (?:the )?(?:processing|use) of my|"
    # German
    r"abmelden|abmeldung|abbestell\w*|austragen|"
    r"keine (?:weiteren |weitere )?(?:e-?mails?|mails?|nachrichten|werbung|newsletter)(?: mehr)?\b|"
    r"nicht mehr (?:an)?(?:schreiben|kontaktieren|mailen|anmailen)|"
    r"(?:schreiben|mailen|kontaktieren) sie mich (?:bitte )?nicht|"
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
_NEGATED = re.compile(r"\b(?:don'?t|dont|do not|never|nicht|no|kein|ne|non|niet)\b")
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


def opt_out(subject: str | None, body: str | None) -> str | None:
    """The words in which an email asks not to be emailed again (its line that does, or its subject), or None."""
    lines = own_lines(body or "")
    topic = _SUBJECT_PREFIX.sub("", _plain(subject or ""))
    if lines:
        written, plain = lines[0]
        first = _bare(plain)
        words = first.split()
        short = 0 < len(words) <= 3 and ("stop" in words or "stopp" in words) and not _NEGATED.search(first)
        if first in _ALONE or short:
            return written[:REASON_CHARS]
    if _bare(topic) in _ALONE:
        return (subject or "").strip()[:REASON_CHARS]
    for written, plain in lines:
        if _PHRASES.search(plain) and not _INSTRUCTION.search(plain):
            return written[:REASON_CHARS]
    return (subject or "").strip()[:REASON_CHARS] if _PHRASES.search(topic) else None
