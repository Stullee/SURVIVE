"""0.42.2 (analysis-0.40.1, 5.6): the tools' limits, sets and fixed texts, split out of tools.py.

One source for every number a tool keeps and a prompt states (tests/cycle/test_text_limits.py): what a reply, a call,
a read and a result hold, the caps of the inbox, the workshop and a draft; the sets of tools a cycle is offered by its
channels and its kind (ETSY_TOOLS to MARKETING_TOOLS and BUILDING_TOOLS) and how they run (MAKERS, CHECKING_TOOLS,
CALLING_TOOLS, READ_ONLY_TOOLS); and the fixed words and patterns some handlers use. tools_specs.py describes the tools
with them, and tools.py and the handlers' modules keep them.
"""

from __future__ import annotations

import re

from ..integrations import etsy
from . import store

MAX_RESULT_CHARS = 4_000
# 0.40.3 (analysis-0.40.1 codebase, T7): the largest whole number a field takes, maximum or not: an id beyond what
# SQLite holds (2^63) failed some 15 tools with OverflowError, and no reason
WHOLE_MAX = 2**62
RESULT_CHARS = {"memory_read": 4_400}  # 0.12.0: a memory file whole (lessons: 4,000 bytes) with its heading
# A work reply's length (prompts takes it from here). JSON specs and German text take about 1.7 characters a token in
# it, so a text that can't be written in parts gets at most ONE_REPLY_CHARS: what fits in one reply beside its call's
# other fields. One workspace_write at 4,000 (until 0.11.1) was cut off again and again; 0.12.0: so were payloads of
# 8,000 characters, emails of 5,000 and listing descriptions of 4,000, which no reply could hold.
WORK_MAX_TOKENS = 2_000
ONE_REPLY_CHARS = int(WORK_MAX_TOKENS * 1.7) - 900  # 2,500
# 0.15.0: one call's texts together, beside its JSON and a line of text (a request_approval's could total 5,240).
CALL_CHARS = int(WORK_MAX_TOKENS * 1.7) - 400  # 3,000
WRITE_CHARS = ONE_REPLY_CHARS
DESCRIPTION_CHARS = min(etsy.DESCRIPTION_CHARS, ONE_REPLY_CHARS - 500)  # a listing's other fields come with it
READ_DEFAULT_CHARS = 3_000
# 0.40.3 (analysis-0.40.1 codebase, T3): what fits in a result (MAX_RESULT_CHARS) beside the header, the data marks
# and the "More from offset" line. At 6,000 the result was cut near 3,870 with that line, and the header still said
# "characters 0–6,000": the agent believed it read 2,100 characters it never saw.
READ_MAX_CHARS = 3_500
READ_ROOM = 200  # a read's header and data marks without its path (twice in them) and what it says of a made file
MAX_TOOL_CALLS_PER_TURN = 4  # a reply's tool calls that run (write_journal besides them); the rest are skipped
MAX_UNREAD_MESSAGES = store.MAX_UNREAD_MESSAGES  # 0.40.3 (PL3); 0.42.0: refused by preconditions.UNREAD
MAX_NEW_UPGRADES = 5
SANDBOX_STRIKES = 3
INBOX_SIZE = 15
INBOX_CHARS = 3_500
EMAIL_READ_CHARS = 3_000
MAX_ACTION_CHARS = 12_000  # the approvals table's limit for an action
ELSEWHERE_SHOWN = 3  # 0.39.2: the files of a missing file's name a refusal names
FOLDER_SHOWN = 40  # 0.40.1: the entries workspace_list shows of one folder, its newest files first
DONE_WORDS = 2  # 0.39.2: a step said done says what was done in at least as many words
LOOK_PIXELS = 1_000  # the longer side of a picture the agent looks at: about 1,000-1,300 input tokens
# 0.40.0 (upgrade request #12): what the agent saw on a paperback's fresh pages (qa), for the owner's request card
QA_CHARS = 900
QA_WORDS = 20
DESCRIPTION_MAX = 2_000  # the approvals table's limit for a request's description
DRAWN_ROOM = 1_200  # a KDP check's own words and its cover's, before the pages it drew
CATEGORIES_SHOWN = 10  # etsy_categories' answer, shortest paths first
DEPARTMENT = " (a whole department: too broad for a listing)"
# Making files takes a moment: these run sealed, but outside the database transaction the other tools share.
MAKERS = frozenset({"make_document", "make_spreadsheet", "make_image", "resize_image", "make_cost_statement"})
# 0.25.0: makes a KDP book's cover and reads and draws its files before it asks the owner: sealed, outside the database
# transaction the other tools share, which it opens itself for the request.
CHECKING_TOOLS = frozenset({"propose_kdp_book"})
GUIDES = (
    "documents",
    "spreadsheets",
    "listing_photos",
    "workshop",
    "etsy",
    "ventures",
    "email",
    "pinterest",
    "bluesky",
    "printify",
    "website",
    "blog",
    "statements",  # 0.20.0: make_cost_statement's
    "kdp",  # 0.25.0: Amazon KDP's books
)
WORKSHOP_TOOLS = frozenset({"workshop"})  # offered only when the owner's options allow workshop runs
# Offered only with an Etsy shop (demand_note 0.12.0: a product line's first listing needs one).
ETSY_TOOLS = frozenset({"etsy_categories", "propose_etsy_listing", "etsy_listing", "propose_etsy_edit", "demand_note"})
# Offered only with the owner's Pinterest account and an Etsy shop (0.13.0, Phase E2): a pin links to a live listing.
PINTEREST_TOOLS = frozenset({"pinterest_boards", "propose_pin"})
# Offered only with the Bluesky account the owner made for Ember (0.19.0).
BLUESKY_TOOLS = frozenset({"bluesky_posts", "propose_bluesky_post"})
# Offered only with the owner's Printify account and an Etsy shop (0.13.0, Phase E4): a product becomes a listing there.
PRINTIFY_TOOLS = frozenset({"printify_catalog", "propose_printify_product", "propose_printify_edit"})
# Offered only when the owner switched Amazon KDP on (0.25.0): the book they publish at KDP, from the agent's spec.
KDP_TOOLS = frozenset({"propose_kdp_book"})
# Offered only when the owner switched their website on (0.13.0, Phase E3): its pages.
SITE_TOOLS = frozenset({"site_page"})
# Offered only when the owner switched their blog on (0.14.0): posts and the link page, uploaded once they approve.
BLOG_TOOLS = frozenset({"propose_blog_post", "propose_link_page"})
# Offered only when Ember has a mailbox (the fake one in dry run, the configured one live).
MAIL_TOOLS = frozenset({"email_inbox", "email_read", "mark_opt_out", "propose_email", "inquiry_done"})
# 0.45.1 (upgrade request: an email_read for KDP's notices): reading an email is a marketing cycle's too, as 0.37.7 made
# reading a channel's numbers (CHANNEL_READS). Live, a cycle the owner asked to read KDP's "Attention needed" emails #64
# and #65 about live books #140 and #141 saw their subjects in MAIL and had no email_read: it could only tell the owner
# they existed. mark_opt_out comes with it: a read email lets an approved one to its sender go out (MAIL's "waits until
# you read email #N"). Answering (propose_email, inquiry_done) and the inbox's list of read emails too stay an ordinary
# cycle's (MAIL and the owner name an email's number), and a venture cycle decides a venture: it has none of them.
MAIL_READS = frozenset({"email_read", "mark_opt_out"})
# Offered only in venture cycles (0.10.0; evidence 0.12.0: a venture's case, which grades pages any research found;
# venture_case 0.13.0: its numbers).
VENTURE_TOOLS = frozenset({"brainstorm", "evidence", "venture_case"})
# Offered only while the owner's library holds documents (0.12.0).
LIBRARY_TOOLS = frozenset({"knowledge_search", "library_read"})
# Offered only in ordinary cycles (0.12.0): a venture cycle researches and decides, so its prompt no longer carries
# the tools for building and selling (making and looking at files, the workshop, the shop, email and Reddit). They
# belong to ordinary cycles, like the legs they serve. 0.35.0: so do the changes to the plan tree's steps (plan_step;
# laying out a roadmap of Ember's own, milestone_plan, retired: the plan tree replaces it). 0.39.1: plan_step is every
# cycle's, a venture cycle's to make a step of its ventures wait (VENTURE_VARIANTS).
ORDINARY_TOOLS = (
    frozenset(
        {
            "make_document",
            "make_spreadsheet",
            "make_image",
            "resize_image",
            "make_cost_statement",
            "look",
            "workshop",
            "draft",
            "propose_reddit_post",
        }
    )
    | ETSY_TOOLS
    | PINTEREST_TOOLS
    | BLUESKY_TOOLS
    | PRINTIFY_TOOLS
    | SITE_TOOLS
    | BLOG_TOOLS
    | MAIL_TOOLS
    | KDP_TOOLS
)
# 0.37.7: reading a channel's numbers brings no buyer: any cycle with the account but a venture cycle has it. Live, an
# ordinary cycle the owner asked how posts #43 and #44 did had no bluesky_posts, and answered that it couldn't say.
CHANNEL_READS = frozenset({"bluesky_posts"})
# 0.28.0: the tools that bring buyers to a line's listings (a pin, a Bluesky post, a blog post, the link page): a
# marketing cycle's (lines.py), and an ordinary cycle's only while no marketing cycles run (the owner's share is 0, the
# burn mode, nothing live to market). A Reddit post stays an ordinary cycle's too (a first test of demand, often).
MARKETING_TOOLS = (PINTEREST_TOOLS | BLUESKY_TOOLS | BLOG_TOOLS) - CHANNEL_READS
# 0.28.0: what a marketing cycle doesn't carry: making documents, spreadsheets and statements, the workshop, new
# listings and products, KDP, answering email and the owner's site pages (an ordinary cycle's work). 0.39.0: a change
# of a Printify product is a marketing cycle's too, as a change of a listing is. 0.45.1: demand_note, which serves a
# line's first listing (a demand step takes an ordinary cycle), makes room for reading an email (MAIL_READS): a
# marketing cycle's fixed prompt stays under 80% of an ordinary one's.
BUILDING_TOOLS = (
    frozenset(
        {"make_document", "make_spreadsheet", "make_cost_statement", "workshop", "propose_etsy_listing", "demand_note"}
    )
    | (PRINTIFY_TOOLS - {"propose_printify_edit"})
    | KDP_TOOLS
    | (MAIL_TOOLS - MAIL_READS)
    | SITE_TOOLS
)
# Model calls of their own (and, 0.12.0, the Etsy market probe of a demand note): they need the network, and no
# database transaction is held meanwhile.
CALLING_TOOLS = frozenset({"research", "workshop", "brainstorm", "draft", "demand_note", "printify_catalog"})
# 0.39.4: the tools that only read: they change no record, file, request or message, and cost nothing. A call the
# model wrote inside its journal runs only when it is one of these (run): a journal can quote someone else's words
# too. memory_read (a replace needs it), email_read (it marks the email read) and look (a picture) are left out.
READ_ONLY_TOOLS = frozenset(
    {
        "workspace_list",
        "workspace_read",
        "project_list",
        "knowledge_search",
        "library_read",
        "guide",
        "email_inbox",
        "etsy_categories",
        "etsy_listing",
        "pinterest_boards",
        "bluesky_posts",
    }
)
WORKSHOP_INPUTS = 5  # files handed over to one workshop run
WORKSHOP_INPUT_MB = 10  # their size together
WORKSHOP_FOLDER = "workshop/out"  # where a run's files go when it names no folder (0.42.1: one source)
# 0.12.0: a long file written in one call of its own (draft; prompts takes these from here). A work reply holds only
# ONE_REPLY_CHARS of a text, and every part written through the conversation is read again by each later step.
DRAFT_MAX_TOKENS = 8_000
DRAFT_CHARS = DRAFT_MAX_TOKENS * 3  # what a draft holds at least (3 characters a token)
DRAFT_BYTES = DRAFT_MAX_TOKENS * 4  # 0.15.0: the room a draft needs in a file (4 bytes a token)
DRAFT_SOURCES = 5  # workspace files a draft builds on
DRAFT_SOURCE_CHARS = 24_000  # their text, together
FIRST_CONTACT = (
    "First email to this address: Ember never received an email from it whose sender was verified. Cold advertising "
    "emails are illegal in Germany (§ 7 UWG)."
)
REDDIT_NOTE = (
    "After you approve, the dashboard opens Reddit with this text filled in: post it from your own account, then "
    "mark it done with the link. Check the subreddit's rules on AI-written content and self-promotion first."
)
KDP_NOTE = (
    "Check the files before you approve. Once published at KDP, mark it done with the book's link at Amazon, and "
    "record its royalties in the ledger."
)
_SITE = re.compile(r"^(?=.{4,60}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
# 0.12.0: a research question asked again within this many days is answered from before (the last REPEAT_LOOKBACK
# research calls are compared).
RESEARCH_REPEAT_DAYS = 30
REPEAT_LOOKBACK = 300
_WRAPPED = re.compile(r'<data src="research" id="([^"]+)">\n(.*)\n</data id="\1">', re.DOTALL)
MAX_SALES = 100_000  # a month, in a numeric business case (0.13.0)
# 0.13.0: the largest amount each number of a venture_case takes (euros, hours or dollars).
CASE_LIMITS = {
    "price_eur": 100_000,
    "unit_cost_eur": 100_000,
    "monthly_costs_eur": 1_000_000,
    "setup_eur": 1_000_000,
    "owner_hours": 744,
    "api_usd": 10_000,
}
_PLAIN_NUMBER = re.compile(r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")  # evidence's values (0.12.0)
_DOCUMENT = re.compile(r"\.(?:pdf|docx?|xlsx?|pptx?|odt|ods|odp|rtf|epub|zip)$", re.IGNORECASE)
# Etsy's website and its short links, with their subdomains: Etsy's API terms forbid programs reading them. Searching
# them (site 'etsy.com') stays allowed: that reads a search engine's results, not Etsy's pages.
ETSY_DOMAINS = ("etsy.com", "etsy.me")
# Sites that block Anthropic's web tools: the API refuses a search limited to them ("not accessible to our user
# agent", seen live in 0.10.0), and their pages can't be read. (0.10.1)
UNREACHABLE_DOMAINS = ("reddit.com", "redd.it")
UNREACHABLE = (
    "Reddit blocks Anthropic's web tools, so your research can't search or read reddit.com: search without a site "
    "(forums, Q&A and review sites often discuss the same questions), or limit it to another site"
)
_BAD_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]")
