"""learning.adopt: a lesson that says the opposite with an antonym (not a negation) counts as support, and three of them
make the opposite principle 'established'."""
import h, json
from app.agent import learning

agent = h.fresh("r4")
scope = agent.scope()
def case(lesson, subject):
    with agent.db.transaction() as conn:
        learning.save_cases(conn, scope, None, [{"subject": subject, "expected": "e", "happened": "h", "why": "w",
                                                 "cause": "worked", "sure": "medium", "lesson": lesson}], [], h.now(agent))
        return learning.adopt(conn, scope, h.now(agent))
A = "Listings priced above 10 EUR get fewer favorites from German buyers in their first two weeks on Etsy"
B = "Listings priced below 10 EUR get fewer favorites from German buyers in their first two weeks on Etsy"
C = "Listings priced below 10 EUR get more favorites from German buyers in their first two weeks on Etsy"
print(case(A, "bet #1"))
print(case(B, "bet #2"))
print(case(C, "bet #3"))
print(case("Pins bring views to new listings within a week", "bet #4"))
print(case("Pins bring no views to new listings within a week", "bet #5"))
with agent.db.connection() as conn:
    for p in learning.principles(conn, scope):
        print(dict(id=p["id"], conf=p["confidence"], supports=p["supports"], text=p["text"][:70]))
    w1, p1, d1 = learning._shape(A); w2, p2, d2 = learning._shape(B)
    print("word overlap %.2f pair overlap %.2f denies %s %s" % (learning._overlap(w1, w2), learning._overlap(p1, p2), d1, d2))
print(case("Listings priced below 10 EUR get fewer favorites from German buyers in their first two weeks on Etsy!", "bet #6"))
print(case("Listings priced over 10 EUR get fewer favorites from German buyers in their first two weeks on Etsy", "bet #7"))
with agent.db.connection() as conn:
    p = [p for p in learning.principles(conn, scope) if p["id"] == 1][0]
    print("principle #1 now:", p["confidence"], p["supports"], p["text"])
