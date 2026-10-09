"""R5: which thread an email_reply unlock thinks 'the other person started'.

DOCS: "email replies in threads the other person started". policy._started_by_them takes the first message id of the
reply's References (else In-Reply-To) as the thread's root; the reply's References come from mailstore.thread_headers,
which keeps only the last 4 earlier ids. A thread Ember started (a first contact the owner approved), after a few
exchanges:"""

import os
import sys
from pathlib import Path

sys.path.insert(0, ".")
data = Path(os.environ["EMBER_DATA_DIR"])

from app.agent import policy, store  # noqa: E402
from app.integrations import mail, mailstore  # noqa: E402
from app.integrations.mail import IncomingMail  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_agent import tools as calls  # noqa: E402
from tests.test_mail import mail_cycle  # noqa: E402

agent, transport = mail_cycle(data, calls(("email_inbox", {})))
scope = agent.scope()
cycle_id = rows(agent, "SELECT MAX(id) AS c FROM cycles")[0]["c"]
WHO = "kim@shop.example"
now = "2026-09-29T08:00:00Z"
with agent.db.transaction() as conn:
    # Ember's first contact e1 (the owner approved it), and its later answers e2, e3
    for n in (1, 2, 3):
        a = store.insert_approval(conn, scope, cycle_id, now, type="contact", title=f"e{n}", description="d",
                                  payload=f"e{n}", expected_cost="none", expected_benefit="b", executor="email",
                                  action=store.canonical(mail.email_action(WHO, "Partnership?", f"e{n}")))
        mailstore.store_outgoing(conn, scope, approval_id=a, message_id=f"<e{n}@ember>", in_reply_to=None,
                                 references=None, from_addr="ember@example.invalid", from_name="Ember",
                                 to_addr=WHO, subject="Partnership?", body=f"e{n}", now=now)
    # Kim's verified answers p1, p2, p3 (each References the whole thread, as mail clients write it)
    refs = []
    for n, uid in ((1, 10), (2, 11), (3, 12)):
        refs.append(f"<e{n}@ember>")
        incoming = IncomingMail(uid=uid, message_id=f"<p{n}@shop.example>", in_reply_to=refs[-1],
                                references=" ".join(refs), from_addr=WHO, from_name="Kim", to_addr="ember@example.invalid",
                                subject="Re: Partnership?", sent_at=now, body=f"p{n}", body_cut=False,
                                authenticated=True)
        email_id = mailstore.store_incoming(conn, scope, incoming, 7, now, "ember@example.invalid")
        refs.append(f"<p{n}@shop.example>")
    row = mailstore.email(conn, scope, email_id)
    print("Kim's newest email References:", row["references_"])
    in_reply_to, references = mailstore.thread_headers(row)
    print("the reply's References (thread_headers):", references)
    action = mail.email_action(WHO, "Re: Partnership?", "Thanks!", in_reply_to, references)
    print("thread's real first message: <e1@ember> (Ember's first contact)")
    print("policy.match ->", policy.match(conn, scope, {"executor": "email", "action": store.canonical(action),
                                                        "venture_id": None, "type": "contact"}))
    # for comparison: the same reply with the thread's full References
    full = mail.email_action(WHO, "Re: Partnership?", "Thanks!", in_reply_to, row["references_"] + " " + in_reply_to)
    print("with the full References ->", policy.match(conn, scope, {"executor": "email", "action": store.canonical(full),
                                                                    "venture_id": None, "type": "contact"}))
