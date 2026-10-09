"""knockouts.cold_words on realistic phrases: what plans cold outreach and is found, and what isn't."""
import sys; sys.path.insert(0, ".")
from app.agent.knockouts import cold_words
plans = [
    "Cold emails to 200 agencies a week",
    "Contact HR managers on LinkedIn with a free sample",
    "Email 200 Etsy sellers a day offering our SEO audit",
    "DM 50 Instagram influencers a day with a discount code",
    "Message potential customers on Instagram who never followed us",
    "Reach out to wedding bloggers by email and offer a commission",
    "Send emails to Etsy shop owners offering listing photos",
    "Write to photographers asking them to try our presets",
    "Wir schreiben Hochzeitsplaner per E-Mail an",
    "Wir kontaktieren Kunden per WhatsApp",
    "Pitch our planners to teachers by email",
    "Wir rufen täglich 20 Firmen an",
]
fine = [
    "No cold outreach: buyers find the listings through Etsy search",
    "Customers can contact shops through Etsy's messages",
    "Buyers who ordered can message the shop with questions",
]
for p in plans:
    print("PLAN  found=%-30r %s" % (cold_words(p), p))
for p in fine:
    print("FINE  found=%-30r %s" % (cold_words(p), p))
