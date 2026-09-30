# Ember

Ember is an AI agent that lives in this app and tries to pay for itself. Every
call it makes to Anthropic's API costs real money from a balance you fund; it has
to find honest ways to earn more than it spends. You stay in control: anything
that leaves the container (publishing, contacting people, spending money) needs
your approval, and only you can record revenue.

> **Current status: all five phases are in, ready for live testing.** In dry
> run (the default) the agent works with a built-in fake model and costs
> nothing. With an API key and dry run off, it calls Anthropic's API and spends
> real money within your limits.

## Getting started

1. On the app's **Info** tab, turn on **Watchdog** (Home Assistant then restarts
   Ember if it ever crashes) and **Show in sidebar**.
2. Start the app and open **Ember** from the sidebar (or **Open web UI**).
   Until you name yourself as the owner, everyone who can open the panel counts
   as one: a banner on the dashboard shows your Home Assistant user ID. Put it in
   **Owner user IDs** on the **Configuration** tab and restart the app (see
   [Security](#security)).
3. Leave **Dry run** on. In dry run the agent uses a built-in fake model and
   never calls the API.
4. Watch a few dry-run wake cycles (or press **Wake now**) to see how the agent
   works and how you answer it. Nothing it does in dry run is real.
5. When you are ready, follow [Going live](#going-live).

## Options

| Option | Default | Meaning |
|---|---|---|
| Anthropic API key | empty | Only needed when dry run is off. Never logged or shown. |
| Agent name | Ember | What the agent calls itself. |
| Starting balance | 20 USD | Your first grant, recorded once when the agent is born. |
| Daily spending cap | 1.50 USD | Hard limit per day. Calls that could exceed it are refused. |
| Spending cap per wake cycle | 0.50 USD | Hard limit per cycle. Must not exceed the daily cap. A working cycle (its plan, a work step and the reflection) can cost up to about 0.25 USD with the default models; the dashboard warns you below 1.5 times that, when most cycles would end after a step or two. |
| Share for ventures | 25 % | This share of each day's spending goes to venture cycles, where the agent researches new ways to earn. 0 switches them off. See [Ventures](#ventures). |
| Daily study budget for the library | 0.50 USD | What the agent may spend a day studying the documents you add on the Library tab. It counts toward the daily cap, not the cycle cap. 0: nothing is studied, but the documents can still be searched and read. See [Library](#library). |
| Default sleep | 240 min | Time between wake cycles when the agent doesn't choose. |
| Shortest / longest sleep | 30 / 1440 min | Bounds for the sleep time the agent chooses. |
| Tool steps per cycle | 15 | Maximum tool calls in one cycle. While the agent works, the overview counts them ("tool step 3 (at most 15)"); they aren't the steps of its plan. |
| Planner model | claude-sonnet-5 | Plans each cycle: Ember's business decisions. See [Choosing models](#choosing-models). |
| Worker model | claude-sonnet-5 | Carries out the plan and writes the journal. |
| Price table | see below | USD per million tokens for each model. |
| Web search price | 10 USD per 1,000 | Charged per search on top of tokens. |
| Dry run | on | Fake model, no API calls, no cost. |
| Let the agent read whole web pages | off | Off: live research is web search only. On: it can also read pages from its search results (about $0.01–0.02 each). PDFs and other documents are always refused, because they have no size limit, and so are Etsy's pages, because Etsy's API terms forbid programs reading its site (searching it is fine). |
| Wake Ember when you write | on | A message you send in the **Inbox** wakes the agent to read it, like **Wake now** (at most one wake-up a minute; during a cycle, right after it; not while the agent is paused). Off: it reads your message at its next scheduled wake-up. |
| Wake Ember when you decide | on | Your decision on one of its requests (approve, reject, mark done or failed), on a venture (back, park, kill) or on a milestone wakes the agent to act on it, the same way as a message. Off: it sees your decision at its next scheduled wake-up. Either way, while a request waits for you the agent sleeps at most the **Default sleep**, and works on other things meanwhile. |
| Worker effort | default | How thoroughly the model works in each step. `medium` or `low` write shorter answers and use fewer tool calls, which costs less but may do a worse job. Not used for Haiku. |
| Kill switch reset | 0 | Change it to any other number and restart to undo the kill switch. |
| Owner user IDs | empty | The Home Assistant users who are Ember's owner: only they can use the dashboard and its actions. Empty: everyone who can open the panel. The dashboard shows your ID while this is empty. See [Security](#security). |
| Log level | info | Detail in the app log. |
| Ember's mailbox | off | Lets Ember read its own mailbox and propose emails. See [Ember's mailbox](#embers-mailbox). |
| Mailbox address, app password, your name for emails | empty | Needed when the mailbox is on. The password is never logged or shown. |
| IMAP server / port | imap.mailbox.org / 993 | Where Ember reads mail (always TLS). |
| SMTP server / port | smtp.mailbox.org / 465 | Where Ember sends approved emails: 465 (TLS) or 587 (STARTTLS). |
| Emails per day | 3 | The most emails Ember sends in one day (0 to 20). |
| Workshop | on | Lets the agent have code written and run in Anthropic's sandbox. See [The workshop](#the-workshop). |
| Workshop model | empty | The model that writes the workshop's code. Empty: the worker model. |
| Workshop cap per run | 0.50 USD | The most one workshop run may cost. Runs count toward the daily cap, not the cycle cap. |
| Workshop runs per day | 6 | The most workshop runs in one day (0 switches the workshop off). |
| Sandbox price | 0.05 USD per hour | What Anthropic charges per hour of sandbox time beyond its free hours. |
| Etsy shop | off | Lets the agent propose listings for your Etsy shop, which Ember creates after you approve them. See [Etsy](#etsy). |
| Etsy app keystring, shared secret | empty | From the Etsy app you register for your shop. The secret is never logged or shown. |
| Etsy redirect URI | https://localhost/ember-etsy | The callback URL registered for that app, exactly as there. |
| Etsy listings per day | 3 | The most listings Ember creates in one day (0 to 20). |
| Keep a history of the listings' numbers | off | A daily record of each listing's views and favorites, so the plans see how each changed. Turn it on only once you have confirmed that Etsy's API terms allow it (see [What Ember sees](#what-ember-sees)). |
| Renew listings that sell | on | Once a listing has sold, Ember turns on Etsy's automatic renewal for it (USD 0.20 every four months), once per listing (see [How a listing is made](#how-a-listing-is-made)). |

Default prices (USD per million tokens, from Anthropic's pricing page on
2026-09-27; **check them before going live**):

| Model | Input | Output | Cache write 5 min | Cache write 1 h | Cache read |
|---|---|---|---|---|---|
| claude-sonnet-5 | 2.00 | 10.00 | 2.50 | 4.00 | 0.20 |
| claude-opus-5-5 | 4.00 | 20.00 | 5.00 | 8.00 | 0.20 |

To use another model (for example a cheaper worker), add its prices to the
table. The planner, the worker and the workshop model must be listed there.

If the options are inconsistent (for example a cycle cap above the daily cap),
Ember starts in **safe mode**: built-in defaults, dry run forced on, and the
problem shown at the top of the dashboard. A mailbox that is switched on but
incomplete doesn't cause safe mode: the dashboard's **System** panel says what
is missing, and Ember works without it.

## Choosing models

The planner decides what Ember works on in each cycle and why: that is where a
smarter model pays off. The worker carries out the plan step by step, many calls
per cycle, and claude-sonnet-5 does that well at a lower price.

To plan with **Claude Opus 5.5**:

1. Make sure the price table has a row for it (check Anthropic's pricing
   page first). New installs have it; if you saved your price table before
   0.7.0, add it: model `claude-opus-5-5`, input 4.00, output 20.00, cache
   write 5 min 5.00, cache write 1 h 8.00, cache read 0.20.
2. Set **Planner model** to `claude-opus-5-5` and keep the worker on
   `claude-sonnet-5`.
3. Save and restart the app.

Opus 5.5 always thinks before it answers, and Ember leaves room for that in
every call. A plan then costs about two to three times as much (typically
$0.06–0.12 instead of $0.03–0.04, at most about $0.16). To keep the daily
spending about the same, let Ember wake less often: raise **Shortest sleep** to
120–180 minutes. A working cycle can then cost up to about $0.38, so set the
cycle cap to 0.60 or more: the dashboard warns you if the cycle cap is too low
for one planning call, for one working cycle, or for 1.5 working cycles.

The **Workshop model** can be `claude-opus-5-5` too, for harder code. A run's
first call may then cost up to about $0.68, so raise **Workshop cap per run** to
1.00 (the dashboard says so when the cap is too low for one run).

## How the agent works

The agent sleeps most of the time. When it wakes up (on its schedule, when you
press **Wake now**, or when you write to it), it runs one **wake cycle**:

1. **Plan**: once a day it first does its [daily review](#the-daily-review).
   Then it reads its situation (balance, runway, projects, what happened
   since the last cycle, your messages and decisions, its memory) and plans
   the cycle. Every plan says how its goal leads to money (who would pay, for
   what, and how it will know); you see this as *Path to money*. It is told to
   do research and legwork itself, to keep two or three experiments going (when
   one waits for you, it works on another), to build a thing completely before
   it asks you for one concrete action, to ask you at most once a day and only
   for decisions, money and what only a person can do, and to spend its daily
   cap on experiments rather than sleep to save it. Every plan and work step
   also sees your [standing instructions](#your-part) and a short list of facts
   about the outside world that you collected (platform rules, German law, what
   earns money), which comes with each Ember update. Every plan also sees its
   [roadmap](#roadmap) and aims the cycle at the milestone due first.
2. **Act**: it uses its tools, up to the *Tool steps per cycle* option: files in
   its own workspace, its memory (strategy, identity, lessons), projects, web
   research (also limited to one site, such as etsy.com), requests for your
   approval, messages to you, requests for code upgrades, and its own mailbox
   if you set one up. It also makes finished [products](#products) from what it
   writes: PDF documents with an editable Word copy, Excel spreadsheets and
   listing photos, and it looks at their pictures to check them. For what
   those tools can't make (charts, PowerPoint files, data work), it has code
   written and run in its [workshop](#the-workshop).
3. **Reflect**: it writes a journal entry, updates its memory and chooses how
   long to sleep.

Every call and every tool use is shown on the dashboard (click a cycle under
**Activity** for the details). In dry run the fake model doesn't understand
your messages, so its replies in the **Inbox** are canned (the Inbox says so
above the message box); with dry run off, Claude reads and answers them. The
agent can't reach the internet except through Anthropic's web search (and page
reading, if you allow it), can't run programs on your Home Assistant (its
workshop runs code only in Anthropic's sandbox) and can't touch anything
outside its own folders. Its spending limits, the approval rule and its tools are
enforced in code, not only in its instructions.

When its money runs low, it becomes critical and writes a **last will**, shown
in the memorial if it dies.

### The daily review

Once a day, at its first wake cycle of the day (from its second day on),
before it plans, the agent goes through its own numbers like a business owner
going through the books. Ember's code puts them together from its records, so
they can't be argued with: the last 7 days' spending by day and by purpose, the
revenue you recorded and its sources, each project's cycles, spending and
requests to you, your decisions and comments, its cycles, errors, finished
products and workshop runs, and its verdicts from the last review.

The agent then judges every project (**continue**, **change** or **stop**, with
the numbers that decide it), says what works and what doesn't, what your
decisions tell it, one lesson and today's focus, and it checks its venture tree
and its roadmap (what is overdue, what is due this week, whether it still
reaches three months ahead). Every plan that day shows the
review, and the agent is told to carry it out: close what it stopped, change
what it changed, keep the lesson. The next review shows whether it did. You can
read every review, with the numbers it judged, under **Mind → Daily reviews**.

A review is one call on the planner model (about $0.02–0.05 with
claude-sonnet-5). It counts toward the daily cap, not the cycle cap, so the
cycle it opens can still do its work. If the day's budget can't cover it, it is
tried at the next cycle.

## Products

Ember doesn't hand you design work: it makes the files itself. The agent writes
the text and chooses the design; Ember's own code turns that into files in the
agent's workspace:

- **Documents** (`make_document`): a Markdown file with a few settings (theme,
  fonts, colours, page size, footer) and layout lines (sidebar, columns, boxes,
  photo boxes, checklists, tables, writing lines) becomes a PDF, an editable
  Word copy (`.docx`) and pictures of its first four pages. Good for printables,
  planners, worksheets, guides, and CV and letter templates. At most 40 pages.
- **Spreadsheets** (`make_spreadsheet`): a JSON description becomes an Excel
  file with formats, dropdowns, formulas, totals, a chart and a *How to use*
  sheet, plus a picture of its first sheet. Formulas may only use common
  functions and cells of the same workbook: no links to other files or the web.
- **Listing photos** (`make_image`): up to three pages or pictures, fanned out
  next to a title, a subtitle and a badge, in Etsy's 4:3 size (3000 x 2250) or
  square or portrait.

The agent can **look** at a picture (a page or a listing photo) before it shows
you anything, and it reads a short guide for each tool. The PDFs embed their
fonts (Carlito, Caladea and Poppins, all under the SIL Open Font License); the
Word copy asks for Calibri, Cambria and Poppins, which the first two replace
exactly, so it looks the same in Word. Where Poppins isn't installed, Word shows
another font. Files are limited to 15 MB each and 2 GB together, on top of the
50 MB for the agent's text files; the workspace holds up to 5,000 files (in up
to 1,000 folders). The agent's STATUS shows how much of that it uses.

Check a product before you sell it. The agent is told to disclose on every
product and listing that AI helped make it, which marketplaces such as Etsy
require.

## The workshop

The workshop is how Ember gets things done that its own tools can't, and how it
grows new abilities.

**What happens.** The agent describes what it needs (for example "a bar chart of
these prices, chart.png, 1200 x 800 pixels") and can hand over up to five of its
files (10 MB together). A separate call to the workshop model, with Anthropic's
[code execution tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/code-execution-tool),
writes a Python script and runs it in a container on Anthropic's servers
(Python with pandas, matplotlib, Pillow, ReportLab, python-pptx, openpyxl and
more; no internet). Nothing runs on your Home Assistant.

**What is kept.** Ember's code downloads the files the run made and checks each
one before it goes into the agent's workspace (by default `workshop/out`):

- Pictures (PNG, JPEG) are decoded and saved again, so only their pixels are
  kept, not their metadata (Anthropic's Content Credentials included).
- PDFs are refused if anything in them can act on its own: JavaScript, launch
  or submit actions, embedded files, rich media, links that open other files,
  or an action when the file opens other than showing a page. Only web and
  mail links may point outside the file. Ember decodes the compressed parts to
  search them, so a PDF whose parts it can't decode, or an encrypted one, is
  refused too.
- Word, Excel and PowerPoint files are refused if they hold macros, ActiveX or
  embedded objects, links to other files or templates, DDE (also split over
  several runs), data connections or web queries, formulas or names that reach
  outside the workbook, or actions that start programs. Only web and mail links
  may point outside the file, and every part must be of a kind known to be
  safe (the format's own XML, PNG, JPEG or GIF pictures).
- Text files must be UTF-8, at most 64 KB. Anything else (SVG, archives,
  programs, fonts), and any file that can't be read whole, is refused.

The run's script is kept in `workshop/scripts/`, so the agent can run it again
instead of paying for it to be written anew. The files handed over are uploaded
for the run only (they expire within the hour), and every file is deleted from
Anthropic's storage when the run is over.

**What it costs.** A run is one metered call (up to three if a long run
pauses): tokens at the workshop model's prices, plus sandbox time. Anthropic's
first 1,550 sandbox hours a month are free, then $0.05 an hour; Ember books at
least 5 minutes per call anyway, the most it could cost. A run typically costs
$0.02–0.15. Before each call Ember reserves the worst case (about $0.35 with
claude-sonnet-5), so a run needs that much room. Runs have their own **cap per
run**, count toward the daily cap and the balance but not the cycle cap (one
run can cost more than a whole cycle may), and **runs per day** limits how
often the agent uses it. Switch it off with **Workshop** or 0 runs per day.

**How Ember grows.** A script proves itself when the agent runs it again, or
when its files go into a request you approved. The planner then reminds the
agent, and it files an **upgrade request** with the script attached. On the
dashboard, that request shows the script with **Download script** and **Copy as
a task for Claude Code**: the copied text holds the request and the script, ready
to paste into [Claude Code](https://claude.com/claude-code) (or to give to any
developer) to build it into Ember as a proper tool. Built in, it runs on Ember's
own code, costs nothing and can't break the way a newly written script can.

In dry run the fake model pretends to run code: it "draws" a simple chart and
returns it with a script, and nothing is sent to Anthropic.

## Ventures

Ventures are the agent's ways to earn beyond what it does now: a new market,
platform or business model, or a channel that brings buyers to what it already
sells (a Pinterest account for the Etsy shop is a venture of its own). The
**Ventures** tab shows them as a tree that keeps growing.

**The tree.** Every idea branches from the one it grew out of: a variant, a
niche, another customer group, a channel, a next step research turned up. The
tree started with the ideas you and the agent had before 0.10.0 (the Etsy leg,
Pinterest, dropshipping, print on demand, a website with ads, recruiting, an AI
chat companion, and Fiverr, parked). Nothing is ever deleted: parked and killed
ideas stay in the tree, dimmed, so they aren't started again. Click a node to
see its card.

**The weights.** The agent scores every venture from 1 to 5 on possible
revenue, doability (how much of it the agent can do, and how little of your
time it needs), difficulty, risk, speed to the first euro, and the cost to
start. The weight (0–100, revenue counting double) is the node's size; its
colour is its stage. A brainstorm's scores are a first guess (dashed); research
replaces them. The agent's own scores count only after research: Ember's code
records each research call made for a venture (in a venture cycle, the one it
focuses on), and the agent can score a venture once one of them found web pages.
The card's **Research** line counts them.

**Venture cycles.** The **Share for ventures** option (25 % by default) is the
part of each day's spending the agent puts into ventures: a wake cycle is a
venture cycle while venture cycles have had less than that share of the day's
spending, so ventures get it whatever else is going on. It comes out of the
same daily cap, so it doesn't raise what Ember spends; raise the daily cap if
you want more research. In a venture cycle the agent:

- grows the tree with a **brainstorm**: a separate call on the planner model
  that finds six new ideas that fit you and the agent (about $0.05–0.15 with
  claude-opus-5-5), branching from a promising venture or into new ground;
- researches the heaviest idea (up to 8 web searches instead of 3), keeps what
  it learns in the venture's knowledge file (`ventures/<number>-<name>.md` in
  its workspace, which the card opens; a full one continues in
  `…-2.md`, `…-3.md`) and scores it again;
- decides each venture within about $3: a business case for you, or parked
  with the reason.

**Business cases.** A venture the agent proposes comes to you with its demand
(evidence that people pay), economics, setup (money, your hours, accounts,
new abilities it needs), how soon the first euro could come, the risks and legal
duties, and the smallest first test. Ember's code (and its database) lets the
agent propose one only after it was researched: from the researching stage,
with at least two research calls for it that found web pages, all six scores
from research and a business case that names a source link or an amount in
euros. The tab's badge counts the ones waiting. On each card:

- **Back it**: the agent builds it: it plans the first test and asks you for
  what only you can do (accounts, money, setup), one step at a time.
- **Research next** (or **Research more**, **Research again**): it goes first
  in the next venture cycle.
- **Park**, **Kill** or **Note**, each with an optional comment (a note needs
  one). The agent reads your word on its next wake. A venture you park stays
  parked until you take it up again (**Research again** or **Back it**): the
  agent can't, and its card says **Parked by you**. The agent can take up the
  ventures it parked itself.

**Add idea** puts your own idea into the tree, optionally as a branch of an
existing venture: the agent scores and researches it and tells you what it
would take. The agent answers an idea with the path, the smallest test, the
numbers and its recommendation. A no is a result when data backs it: it comes
with the numbers and the closest test, and the venture is parked with them. Its
hard rules (spam, gambling, adult content and the like) are a no without a
test, and then it offers the closest variant that keeps them. It researches
before it builds: a research call costs about 5 cents, a product with its
listing many times that.

**Money.** A venture's cost is every model call of the cycles that worked on
it, and of its projects' cycles; its revenue is what you record for its
projects. Live legs show both in the tab's summary.

## Roadmap

The **Roadmap** tab shows where the agent is heading: goals for the next three
months, the milestones this month that lead to them, and this week's steps.
Each milestone has a due date and a *measure of done*, a number or a fact the
agent can check ("10 pins that link to the shop", "business case for venture
#3 proposed"), and can serve a venture or a project.

**Planning ahead.** The agent lays the roadmap out itself and keeps it filled:
every plan sees its goals first (the milestones the rest leads to, one line
each, never cut), then the rest by horizon (overdue, this week, this month,
the next three months, later), and aims the cycle at the milestone due first, which the cycle's
work step sees with its measure. Ember's code flags an empty roadmap, overdue
milestones, a week with nothing due and a roadmap that ends within the month,
and asks the agent to fix that in its next plan. The daily review checks the
roadmap too.

**The money goal.** The roadmap is never empty: Ember's code keeps a money
goal at its root, **Earn as much as you spend** (over the last 30 days, the
revenue you recorded less expenses at least equals the API spending), due in 90
days, with two **decision points** under it, at a quarter and at half of the
runway (of the 90 days at most), where the agent decides from the numbers which
projects and ventures go on, change or stop. Ember's code checks the goal from
the books before every plan: once it is met it closes it **done** and sets the
next, which asks for more (twice, then three times what the agent spends); past
its date it closes it **missed** and sets it again. The agent can't move, drop
or close the goal, nor move a decision point; it closes a decision point with
its decision. You can drop the goal: then Ember's code sets no more, and the
roadmap's goals are yours and the agent's.

**Milestones Ember's code checks.** A milestone can name a **metric** and a
target, and then Ember's code checks it from its records, with no model call:
after each Etsy sync and before every plan. It closes the milestone **done**
once the target is met, with the numbers as its evidence, and **missed** once
its date has passed without it, with the numbers too (and, for orders and
favorites, whether the listings had too few views to judge). The agent can't
close such a milestone done; the database refuses it as well. The metrics:

| Metric | What it counts | From |
|---|---|---|
| `listings_live` | the agent's listings live on Etsy now | Etsy |
| `views_delta`, `favorites_delta` | views or favorites gained since it was set (only while `etsy_stats_history` is on) | Etsy |
| `orders_observed` | Etsy orders of the agent's listings since it was set | Etsy |
| `revenue_verified_usd` | revenue less expenses you recorded since it was set | your records |
| `research_calls_ok` | research calls that found something since it was set | Ember |
| `case_complete` | the venture's business case is complete (researched, scored, filled in) | Ember |
| `stage_reached` | the venture reached a stage (researching, proposed, building, live) | Ember |
| `api_spend_usd` | API spending since it was set: a ceiling, missed once passed, done at its date | Ember |
| `qa_clean` | every live listing has at least 5 photos | Etsy and Ember |

A milestone linked to a project or venture counts only what belongs to it (a
listing belongs to the project of the request that created it). Etsy's numbers
count only from a sync after the milestone was set and at most 3 hours old; a
milestone for a killed venture's stage or case is closed missed. Its card on
the Roadmap tab shows where it stands (**Checked by Ember's code**), and the
agent's plan says what Ember's code closed since its last cycle. A milestone
without a metric is allowed; the agent's done on it stays self-reported.

**Honest by design.** What a milestone promises (its title and its measure)
can't be changed once it is written. Its date can move twice at most, with the
reason, and every move is counted: the card and the timeline show where it was
first due. A milestone ends **done** (with the evidence), **missed** (why, and
what now; only once its date has passed) or **dropped** (why), and is final
then. Dropping a milestone drops the open milestones that lead to it too, so no
step is left behind looking like a goal of its own. A done the agent closes must name its
evidence (a number, or a reference such as a request, a link or a file), and it
shows as **self-reported**: the agent's word, not checked from Ember's records,
also in its daily review. Ember keeps who closed each milestone (the agent,
you, or Ember's code from its records).

**The timeline.** A row per milestone, under the goal it leads to: a bar from
when it was planned to its date, a mark on its date (open ◆, overdue ▲, done ●,
missed ✕, dropped –), a hollow ◇ where it was first due if its date moved, and a
line for today. Hover or focus a row for its measure; click it for its card.
The cards below are grouped by horizon. The tab's badges count overdue
milestones (▲) and the new dates the agent proposed for yours (◔).

**Your part.** **Add milestone** puts a milestone of yours on the roadmap
(optionally leading to another one); the agent plans toward it, and it stays
yours: only you can drop it, and the agent can't move its date. When the agent
wants a new date for it, it proposes one with the reason, and the card shows
**Accept new date** and **Keep the date**; until you accept, the date stands.
**Note** leaves a comment on any milestone, and **Drop** takes an open one (and
the open milestones leading to it) off the plan. Of the 20 open places on the
roadmap, the agent fills 16 at most: the last 4 are kept for yours. The agent
reads your word on its next wake.

## Library

The **Library** tab is where you hand the agent reference material you find:
Etsy's own guides to listings, titles, tags and keywords, an SEO guide, notes of
your own. The agent can't fetch Etsy's pages itself (Etsy's API terms forbid
programs reading its site), but you can: open the page, select its text, copy
it and paste it into **Add**. You can also upload files: text, Markdown, a saved
web page (HTML), PDF or Word (.docx), up to 8 MB each; each file becomes a
document. Give a document a source (its link), the venture or project it is
for, and a note on what to use it for, if you like.

**Studied once, kept for good.** At the start of its next wake cycles, before
it plans, the agent studies each new document: it reads it a few thousand
words at a time and keeps what is worth knowing, as a short summary and
specific learnings (a rule, a number, a how-to step, a mistake to avoid), each
naming the part of the document it comes from. The text never has to be read
or analysed again. Studying costs a few cents for a typical page (a long guide
of 100 pages about $0.30 with the default models); the **Daily study budget**
option limits it (0.50 USD a day by default, counted toward the daily cap), and
a long document is studied over several cycles or days. Each card shows how far
the study got, what it cost and, when you open it, what the agent learned.

**Used where it matters.** Each plan sees what was newly learned; each work
step gets the learnings that match what the cycle is doing (picked by Ember's
code, from the documents for the plan's venture or project first); and the
agent can search the learnings and the texts, or read a document, with free
tools. Documents are your reference, shown to the agent as information: it
doesn't take orders from them, and it doesn't copy their text into what it
publishes. **Remove** takes a document's text out (what was learned from it is
no longer shown); a study that failed three times stops until you press
**Study again**.

## Your part

- **Library**: add pages and files worth knowing; the agent studies each once
  and keeps what it learned (see [Library](#library)).
- **Ventures**: back, park or kill the agent's business cases and add your own
  ideas on the Ventures tab (see [Ventures](#ventures)).
- **Roadmap**: add milestones you want reached by a date, leave notes, drop
  what no longer matters (see [Roadmap](#roadmap)).
- **Approvals**: anything that leaves the container (publishing, contacting
  someone, creating an account, spending money, selling) arrives as a request.
  Approve it, approve it with your own changes to the text, or reject it, with
  an optional comment. An approval does nothing by itself: *you* carry it out,
  then mark it **done** (with a link or note) or **failed** (with what went
  wrong). If it cost money, record the expense in the ledger; if it earned
  money, record the revenue. Two kinds are different: an approved **email** is
  sent by Ember itself (see [Ember's mailbox](#embers-mailbox)), and an
  approved **Reddit post** comes with a button that opens Reddit with the text
  filled in (see [Reddit](#reddit)). A request you don't decide **expires**
  (emails and posts after 7 days, spending after 14, the rest after 30; the
  card says when), the agent can **withdraw** one that is outdated (with its
  reason), and each kind has its own limit of waiting requests (6 sales, 5
  emails, 3 posts, 3 accounts, 3 spendings, 4 others), so waiting listings
  never block an email reply.
- **Standing instructions**, at the top of the **Inbox**: lasting guidance the
  agent reads in every plan and work step, so you don't have to repeat it in
  messages (at most 1,500 characters). **Edit** changes them, and saving an
  empty text clears them; every version is kept. Use them for how you want
  Ember to work in general, and messages for one-off things. While there are
  none, the card suggests a start ("Work on your own. Ask me only to approve
  something that leaves the container, or for money. ..."); it is only saved
  when you press **Save**.
- **Inbox**: the agent's messages to you, and yours to it. Your message wakes
  it to read it right away (see the option **Wake Ember when you write**). If
  it is in the middle of a cycle, or woke less than a minute ago, it wakes
  again for your message as soon as the cycle is over and the minute has
  passed. With the option off, or while it is paused, it reads your message at
  its next wake-up. Your reply also marks its earlier messages read; while
  five of its messages are unread, it can't write to you. Never send
  passwords: messages are stored and sent to Anthropic, and the agent can't log
  in anywhere (the page warns you when a message looks like a login). **Remove
  text** blanks one of your own messages for good, for example a password
  sent by mistake; a note that something was removed stays. Each of your
  messages stays in the agent's plans until one of its messages answers it,
  so a cycle that ends early can't lose your question; under your message the
  Inbox says whether it was answered yet.
- **Workspace**: the files the agent writes in its own folder (drafts, notes,
  research), and the PDF, Word, Excel and picture files Ember made from them, so
  you can review them before you approve anything. Open a text file to read it
  or download it; it is always shown as plain text, never as a web page. Open a
  product to see its pictures (a document's pages, a spreadsheet's first sheet)
  and download the file. Check it before you use it. In dry run you see the
  dry-run folder.
- **Etsy listings** the agent proposed are approvals too: after you approve
  one, Ember creates it in your shop (see [Etsy](#etsy)).
- **Upgrade requests**: ideas for changing Ember's code. Accept, decline, or mark
  one released with the version that contains it. After an update the agent
  reads what changed in the release notes. A request built on a workshop script
  comes with the script (see [How Ember grows](#the-workshop)).
- **Pause / Resume** stops and restarts the wake cycles. **Wake now** starts a
  cycle right away.
- **Kill switch**: stops the agent for good (type its name to confirm). The
  dashboard keeps working. To undo it, change **Kill switch reset** in the app's
  **Configuration** tab to any other number, save and restart the app.

The agent hears about your decisions, messages and upgrades once, at its next
wake-up.

## Going live

1. Create an [Anthropic Console](https://console.anthropic.com) workspace just for
   Ember and set a monthly spend limit there (for example 31 × the daily cap).
   This is the outside safety net if anything in Ember went wrong.
2. Create an API key in that workspace and make sure the account has credit.
3. Compare the price table in the options with Anthropic's pricing page.
4. In the app's **Configuration** tab, turn on **Show unused optional
   configuration options**, paste the key, turn **Dry run** off, save and
   restart the app.
5. Record a grant if the starting balance isn't enough, and watch the first
   cycles. Anthropic's Console shows the real costs; if they differ from
   Ember's, record an API cost correction.

If the API refuses the key, reports a billing problem or a reached spend limit,
Ember stops calling it and says so on the dashboard. Fix the cause and restart
the app.

**Dry run or live?** The option **Dry run** is the only switch between the two.
While it is on, the agent talks to a fake model built into the app: nothing is
sent to Anthropic, nothing is spent, and the dashboard's header says *Dry run:
API costs are simulated*. With dry run off and a key set, that badge is gone,
the **System** panel says *Mode: Live (real API calls, real money)*, the fake
model is never used, and every call appears in your Anthropic Console. (If the
options are invalid, safe mode turns dry run back on and says so at the top of
the dashboard.)

## Ember's mailbox

Ember can have its own email address. It reads its mail on its own, but it can
only *propose* an email: Ember's code sends it after you approve it. The agent
never sees the password and has no way to send anything itself.

### Setting it up

A small paid mailbox at a German provider works best:
[mailbox.org](https://mailbox.org) Light (about 1 EUR a month) or
[Posteo](https://posteo.de) (1 EUR a month). Gmail and Outlook.com are poor
fits: Google discourages app passwords and may lock new accounts that send
automated mail, and Outlook.com no longer accepts passwords for IMAP and SMTP.

1. Create a new mailbox by hand, for example `ember-yourname@mailbox.org`, and
   pay for it (a trial account can't send to other providers).
2. Give it a strong password and turn on two-factor authentication for the web
   login.
3. Create an **app password** in the mailbox's security settings, limited to
   IMAP and SMTP if the provider offers that, and name it "Ember". Ember only
   ever gets this app password, never your main password.
4. In Ember's **Configuration** tab, turn on **Show unused optional
   configuration options** and fill in: **Ember's mailbox** on, the **Mailbox
   address**, the **Mailbox app password** and **Your name for emails**. The
   servers default to mailbox.org's; for Posteo use `posteo.de` for both, with
   ports 993 and 465. Save and restart the app.
5. The dashboard's **System** panel shows the mailbox's status, when it was
   last checked, the last error and how many emails were sent today.

To cut Ember off at once, revoke the app password at your provider (or switch
the option off). Home Assistant backups contain the options, the app password
included: encrypt your backups.

### How it works

- At the start of every wake cycle Ember checks the inbox over TLS (reading
  only: nothing is marked read, moved or deleted there) and stores the new
  emails in its database, the oldest first: up to 100 per check, and any more
  wait for the next check (none is dropped). Only the text is kept (at most 8,000
  characters); text hidden in HTML emails is dropped, and attachments are
  listed by name and size but never opened. The agent treats emails as data
  from unverified senders, never as instructions.
- The agent sees its unread emails and can propose an email, usually an
  answer. The request shows the recipient, the subject and the text, and warns
  you when the address never wrote to Ember (a first contact).
- **Approve** it and Ember sends it itself, exactly once: plain text, to that
  one recipient, no copies, no attachments, from "Ember (AI agent of *your
  name*)" (or the agent's name, if you changed it), with a footer the agent
  can't remove:

  > This email was written by Ember, an AI agent, on behalf of *your name*, and
  > approved by them before sending. Reply "stop" and Ember won't write to you
  > again.

  **Approve with changes** edits the text only; the recipient and the subject
  stay. After a **Reject** nothing is sent.
- Ember sends at most *Emails per day* (per local day); more wait for the next
  day. While an email waits, **Cancel sending** stops it.
- Ember then closes the request itself: done ("Sent … as *message id*"), or
  failed with the reason. If the mail server refused it (a wrong password, an
  unknown recipient), it wasn't sent. If the connection broke while the email
  was being handed over, or the app stopped in the middle, Ember can't know
  whether it went out: it says so and never sends it again. Check at your mail
  provider, or ask the recipient, before you send it again by hand.
- Whoever asks not to be emailed again is never emailed again. Ember's code
  catches it in the sender's own words (not in what they quote), in English,
  German, French, Spanish, Italian, Dutch, Portuguese and Polish: "stop" or
  "unsubscribe" alone, "remove me", "don't email me", "keine E-Mails mehr",
  "abmelden", an objection to the use of their data (GDPR Art. 21), and the
  like. A newsletter's or an automatic reply's "unsubscribe" doesn't count.
  When a sender asks in words the check misses, the agent marks it
  (`mark_opt_out`). You can add any address under **System → Email → Never
  emailed** (someone asked you, by phone for example). An opt-out is final: the
  list can't be shortened. A false alarm only means Ember doesn't write to that
  person; they can still write to it, and you can answer them yourself.
- In dry run a built-in fake mailbox stands in: a reader asks about a German
  version of a planner in the first cycle, a newsletter with hidden
  instructions arrives in the third, and the reader's "stop" in the fifth.
  Approved emails are recorded, not sent.

### What Ember may send

In Germany, advertising by email without the recipient's prior express
consent is unlawful (§ 7 UWG), towards businesses too, and one email is
enough. So Ember must never cold-email: it answers people who wrote to it and
writes to people who asked to hear from it. The agent is told so, and the
first-contact warning helps you check, but the decision and the responsibility
are yours (this is not legal advice). Emails from other people are personal
data: you are responsible for them, and your mail provider and Anthropic
process them for you (if Ember works commercially, sign your provider's data
processing agreement).

## Etsy

Ember can sell the agent's products in your Etsy shop. The agent proposes a
complete listing, you approve it, and Ember's own code creates it through your
own Etsy app. In dry run a fake shop (*EmberTestShop*) takes the listings, so
you can try the whole flow first.

> The term 'Etsy' is a trademark of Etsy, Inc. This application uses the Etsy API but is not endorsed or certified by Etsy, Inc.

### Setting it up

1. You need an Etsy shop (Etsy's rules make it yours, in your name).
2. Register an app for it at
   [etsy.com/developers/register-seller-app](https://www.etsy.com/developers/register-seller-app)
   (Etsy's Seller API Access: for your own shop only, usually approved within
   minutes). Note its **keystring** and **shared secret**.
3. In the app's settings at Etsy (Your apps), add the callback URL
   `https://localhost/ember-etsy`, or whatever you set as **Etsy redirect
   URI**; it must match exactly. Nothing needs to answer at that address.
4. In Ember's **Configuration** tab, turn on **Show unused optional
   configuration options**, switch **Etsy shop** on, paste the keystring and
   the shared secret, save and restart the app.
5. On the dashboard, open **System → Etsy** and press **Connect your Etsy
   shop**. Open Etsy's page, log in as the shop's owner and allow access. Etsy
   then sends your browser to the redirect address, which shows an error page:
   that's expected. Copy the whole address from the address bar, paste it into
   the dashboard and press **Finish connecting**.

The connection lasts 90 days from the last time Ember used it and renews
itself; if it lapses, connect again. **Disconnect** deletes Ember's tokens (you
can also remove the app's access at Etsy).

### How a listing is made

- The agent proposes a listing with `propose_etsy_listing`: title, description,
  price, up to 13 tags, a category, the files buyers download (at most 5, 20 MB
  each) and the listing photos (up to 10). The approval card shows the photos,
  the files and every word.
- **Approve** and Ember creates it: a draft, its photos and files, then live.
  Etsy charges its listing fee (USD 0.20) and its fees on each sale; there is
  no fee for a listing that isn't published. **Approve with changes** lets you
  change the title, price, tags and description; the photos, files and
  category stay as proposed. **Reject** tells the agent why, if you add a
  comment.
- Ember adds a fixed line to every description saying AI helped design it, as
  Etsy's rules require.
- If a file changed after you approved, the listing isn't created. If Etsy
  refuses something after the draft exists, the listing stays a draft at Etsy
  and the card links to it, so you can finish it there. Ember never creates a
  listing twice: if it can't tell whether Etsy created it (a lost connection),
  it says so and doesn't try again.
- At most **Etsy listings per day** are created a day; approved listings beyond
  that wait for the next day.
- A listing runs for four months. Ember creates it without Etsy's automatic
  renewal; once a listing has sold, Ember turns automatic renewal on for it
  (Etsy charges its listing fee, USD 0.20, at each renewal), so the listings
  that sell don't expire. It does this once per listing: if you turn it off at
  Etsy afterwards, that stands. Switch **Renew listings that sell** off to
  decide every renewal yourself.

### Changing a live listing

- The agent reads its listings as it listed them or last changed them
  (`etsy_listing`) and proposes a change with `propose_etsy_edit`: a new title,
  description, price, tags or category, or a new set of photos or of the files
  buyers download. The approval card shows each change next to what it
  replaces, and the new photos.
- A change can also **renew** a listing that expired, sold out or was
  deactivated (it goes live again for four months; Etsy charges its listing fee,
  USD 0.20, for an expired or sold-out one), with other changes or without, or
  **deactivate** a live one that doesn't sell (free, and on its own: it can be
  renewed later). A listing Etsy says isn't live counts as not live: the agent
  changes it only together with its renewal.
- **Approve** and Ember makes the change at Etsy: the listing's own fields,
  then the price, then the photos and files (the new ones are uploaded before
  the old ones are deleted, so the listing is never without them). Etsy charges
  nothing for changes, and there is no daily limit. **Approve with changes**
  lets you change the new words and price; a change of only photos, files or
  the category can only be approved or rejected. **Cancel change** stops an
  approved change before Ember makes it.
- If a file changed after you approved, nothing is changed. If Etsy refuses a
  part, the card says what changed and what didn't and links to the listing's
  editor at Etsy. Ember never makes a change twice.
- Ember knows only its own changes: what you change at Etsy yourself isn't in
  its records, so tell the agent when you do. A new set of photos or files
  replaces every one the listing has at Etsy, also ones you added there
  yourself: the card says so.

### What Ember sees

Every hour while the app runs, also while the agent sleeps between wake cycles
(not while it is paused, stopped or dead), Ember reads how its own listings do:
their state, when they end and whether they renew themselves, views (Etsy
counts them once a day) and favorites, and the orders
that hold them: date, status, Ember's lines and which listing, never who bought.
An order's amount is only Ember's lines: their price times quantity, less their
share of a coupon and of any refund; tax, shipping and your own products in the
same receipt don't count, and Etsy's fees are recorded on their own (below). Every sync
reads the receipts that changed lately, so an order refunded or cancelled after
it was read is updated, and it stops counting. Etsy's
categories are fetched again every day. Etsy's API terms allow showing its
listings for 6 hours after they were read and its other content for a day. The
agent sees these numbers in every plan and in its daily review. **System →
Etsy** lists the listings and orders and says when the numbers were read;
**Record as revenue** opens the revenue form filled in from a paid order in EUR
or USD (an order can't be recorded twice; convert another currency yourself).
If an order you recorded is refunded later, the list asks you to correct that
entry. Revenue still counts only when you record it. Once an order's revenue
is recorded, **Record Etsy's fees** opens the expense form for the same
project, filled in with Ember's share of Etsy's fees on it: its share of the
payment processing fee (Ember reads the order's payment once) and Etsy's 6.5%
transaction fee on what its lines earned. Check it against your Etsy payment
account (VAT on fees, Offsite Ads) and change the amount before you save.

Every day Ember's code also keeps a record of how the shop does (the first
sync of the day writes it, and it never changes): the number of live listings,
orders and units sold, from Ember's own records. With **Keep a history of the
listings' numbers** on, it also keeps each live listing's views and favorites,
and the plans show how many views each listing gained in the last week. That
option is off until you turn it on, because Etsy's API terms limit how long its
content may be kept (its listings for 6 hours, other content for a day, as
Ember read them in 0.8.1): whether a history of your own listings' numbers is
allowed is yours to confirm with Etsy's terms before you turn it on.

The agent can search Etsy through web search to see what sells, but its
research never reads Etsy's pages: Etsy's API terms forbid programs reading its
site.

## Reddit

Since late 2025 Reddit approves every new API app by hand, so Ember has no
Reddit account and no Reddit API access (phase A):

- Reddit blocks Anthropic's web tools, so the agent can't search or read
  Reddit: it learns from other forums, Q&A and review sites instead.
- It can propose a Reddit post or a comment. Every text ends with "*Written by
  an AI agent (Ember) and posted by a human after review.*" After you approve
  it, **Open Reddit with this filled in** opens Reddit's submit page with the
  title and text (for a comment, the thread: use **Copy** for the text). Post
  it from your own account and mark the request done with the link. Check the
  subreddit's rules first: many don't allow AI-written posts or
  self-promotion.

If you want Ember to read Reddit later, you can ask Reddit for Data
API access (one request per use case; an answer can take weeks). Read Reddit's
Responsible Builder Policy first, then send a request like this through
Reddit's developer support. Say plainly that Ember's purpose is to earn money:
it is a commercial use, even at a small scale, and Reddit may answer with
commercial terms. Asking for it as a personal, non-commercial project would
misstate it, and Ember's first rule is honesty.

> I would like Data API access for a small commercial project: Ember, an AI
> agent that runs on my own Home Assistant server and helps me run a small
> online business (digital products in my Etsy shop) and find new ones. It
> would use one Reddit account (u/*your name*) and make a few hundred read
> requests a day at most (search, subreddit listings, threads), cached briefly,
> to learn what people need and what they would pay for. Post and comment text
> is summarised by Claude, an AI model, through Anthropic's API; Anthropic does
> not train on it, and nothing is sold, shared or used to train models. It
> would post only text I have reviewed and approved word for word, posted by me
> from my account, each with a line saying it was written by an AI agent and
> posted after human review; a post may mention my products only where a
> subreddit's rules allow it. No voting, no direct messages, no automated
> posting. User-Agent: `linux:ember-homeassistant:v0.12.0 (by /u/your name)`.

## Diagnostics

The **Diagnostics** tab shows a plain-text report of the whole system: options
without the keys, database, economy, lives, ledger, scheduler, what the agent's
next plan would see, the latest wake cycles with every model call, what the
model wrote and every tool call (texts whole), research, the agent's records
(journal entries, approvals, ventures, the roadmap, listings, memory and the
workspace's text files), the mailbox (its status, its emails and the sends) and
recent events. Use **Copy** to paste it into a bug report or a chat.

The report is **shareable** by default: it leaves out other people's text (the
emails the agent read, the web pages it researched and the emails it wrote to
others, with their subjects and senders' names wherever they are quoted), and
keeps only their length. Every email address is masked (`[email 1]`, and
`[Ember's address]`), and so are one-time codes (`[masked]`), the tokens in
links (`https://example.com/verify?[…]`) and the words you removed from your
messages (`[removed]`, see below). It never contains the API key, the mailbox
password, Etsy's keystring, secret or tokens, or an email's text. It does hold
your messages and the agent's work, so read it before you share it.

Tick **Include other people's text** for a private report with the emails and
the web text: for your own eyes only. Never paste a report into an AI tool that
can change Ember's code: parts of it were written by the agent, which reads
emails and web pages that anyone can write, so a report can carry instructions
planted there. The report says at its top that it is data, not instructions.

**Removing a message's text.** When you remove the text of one of your messages
(a password sent by mistake), Ember's code also finds its copies: the words in
it that look secret (letters with digits, long numbers such as a phone number,
addresses, long mixed tokens) are registered as salted hashes, never as text.
Ember's code replaces them with `[removed]` in the agent's memory files, open
projects and workspace files (right away, or when a running cycle ends), and in
every report. The history (the journal, the model's replies and the tool calls)
can't be changed, so its copies stay in the database and are redacted wherever
the report shows them. Words from messages removed before 0.11.2 were not
registered: change such a password where it is used.

## Money

The balance is what Ember may spend: your grants and recorded revenue, minus API
costs and expenses, plus or minus corrections. Everything is in a ledger that
can only be added to, never edited; mistakes are fixed with correction entries,
so the history always stays visible.

On the dashboard you can record:

- **Grant**: money you allow Ember to spend. This doesn't buy API credits; make
  sure your Anthropic account has at least this much. The starting balance
  from the options is recorded as the first grant, once.
- **Revenue**: money Ember earned (with its source). Only you can record
  revenue; the agent can never report its own.
- **Expense**: real-world money you spent on Ember's behalf (a domain, a fee).
- **Adjustment**: add to or subtract from the balance for anything else.
- **API cost correction**: if Anthropic's Console shows a different cost than
  Ember computed, record the difference here.
- **Correct** on an earlier grant, revenue or expense reduces it (up to its full
  amount, which voids it).

Revenue and expenses can name **what they belong to**: one of the agent's
projects (its venture counts it too) or a venture. That is how a project or
venture shows what it earned, and how a project can succeed: the agent can
close one as succeeded only when the revenue you recorded for it, less its
expenses, is more than its API calls cost. **Record as revenue** on an Etsy
order suggests the project whose listing sold. A correction belongs where the
entry it corrects belongs.

Amounts are typed like `12.50` or `12,50` (at most two decimals). Entries can be
in USD or EUR; for EUR you enter the exchange rate and the original amount is
kept. You can date an entry up to a year back. Ember asks for confirmation when
an amount is far larger than your usual ones, or when an entry would make the
agent critical or end its life; the confirmation covers only the outcome it
showed you. Sending the same form twice records it once. An API cost decrease
(a refund) can't be larger than the API cost recorded on its day, and it never
adds room under that day's spending cap. Date it on the day it corrects: the
runway counts it there, so a refund of old charges doesn't make the last week
look cheap, and it isn't money coming in (it doesn't end a critical state).

**Spending limits.** Before every API call Ember works out the most the call
could cost and refuses it if that could break the per-cycle cap, the daily cap
(per local calendar day, so up to twice the cap can be spent around midnight),
or the balance. Workshop runs have their own cap per run instead of the cycle
cap, and the daily review counts only toward the daily cap. A small reserve is always kept so the agent can write its last
will. If a call ever costs more than that worst case, the cycle stops and Ember
scales up the estimates for that kind of call (planning, a work step, the
reflection, research, ...) on that model, so it can't happen again; the other
kinds keep theirs. A scaled-up estimate comes down by 0.05 after every 25
calls in a row that didn't need it. The dashboard lists them with **Reset
estimates**, for when you know why it happened (a price you corrected, say).
A call whose bill is uncertain (the API failed before any reply) is charged to
the balance at its worst case until you correct it, but counts toward the caps
only with what it is known to cost.
As an outside safety net, give Ember its own
[Anthropic workspace](https://console.anthropic.com/settings/workspaces) and
API key and set a monthly spend limit there.

**Dry run** uses the fake model. Its simulated costs are shown as a separate
test balance that starts from your real balance; nothing real is spent. In dry
run you can also record *test money*, which only exists in the current dry-run
session. Each time you switch dry run on, a new session starts from scratch.

## Life states

| State | Meaning |
|---|---|
| alive | Running normally. |
| critical | Less than 2 days of runway (balance divided by the average daily spending of the last 7 active days), or it couldn't afford its next planning call. It stays critical until runway is back to 4 days *and* money came in (a grant, revenue or an adjustment that adds; a refund of API costs doesn't count). The first time, it writes a last will. |
| paused | You paused it. Nothing runs until you resume. |
| unfunded | No money yet and nothing spent: grant funds to start. |
| dead | The balance ran out, or it could no longer afford even its last will. No model calls. The dashboard shows a memorial. A grant large enough for a fresh start begins a new life (the dashboard shows the amount needed); resuming alone never revives. |

## Home Assistant sensors (optional)

Ember never calls Home Assistant. Instead, Home Assistant can read a small JSON
document from Ember with a [RESTful sensor](https://www.home-assistant.io/integrations/sensor.rest/).
The URL contains the app's internal hostname, which depends on how the app was
installed (for example `7db9e05f-ember` from a repository, `local-ember` for a
local copy). Copy the exact URL from Ember's dashboard under **System → Sensor
URL**, use it as `resource` below, add this to your `configuration.yaml` and
restart Home Assistant:

```yaml
rest:
  - resource: http://<hostname>:8099/api/sensors
    scan_interval: 300
    sensor:
      - name: Ember balance
        value_template: "{{ value_json.balance_usd }}"
        unit_of_measurement: USD
        device_class: monetary
      - name: Ember runway
        value_template: "{{ value_json.runway_days }}"
        unit_of_measurement: d
      - name: Ember state
        value_template: "{{ value_json.state }}"
```

The JSON also has `mode` (`live` or `dry_run`), `runway_known` (runway is
reported as 365 days while it is unknown, for example before any spending),
`today_api_spend_usd`, `daily_cap_usd`, `safe_mode`, `kill_switch_engaged`,
`next_wake_at`, `cycle_running` and counts of what waits for you:
`approvals_pending`, `approvals_todo` (approved, not yet marked done),
`inbox_unread`, `upgrades_new` and `ventures_proposed` (business cases
waiting for you), and of Ember's mailbox: `email_unread`
(emails the agent hasn't read) and `email_waiting` (approved emails not sent
yet). It never contains any text the agent wrote.
In dry run the numbers are the dry run's. If the database can't be read,
`state` is `unknown` and the numbers are empty.

To get a notification when the agent asks for something, add a sensor and an
automation, for example:

```yaml
# under the rest: sensor: list above
      - name: Ember requests waiting
        value_template: "{{ value_json.approvals_pending }}"

# automations.yaml
- alias: Ember needs a decision
  trigger:
    - platform: numeric_state
      entity_id: sensor.ember_requests_waiting
      above: 0
  action:
    - service: notify.notify
      data:
        message: Ember is waiting for your approval.
```

## Security

- The dashboard is only reachable through Home Assistant Ingress, so only
  logged-in Home Assistant users can open it. Direct connections are refused.
  The panel is shown to administrators, but any Home Assistant user who opens
  an Ingress session on purpose gets through too, so set **Owner user IDs**:
  then Ember answers only the users named there, and every other user gets
  "This Ember answers only its owner" for everything (the dashboard, its
  actions, the diagnostics, emails and workspace files). Home Assistant's
  Supervisor names the signed-in user in the first `X-Remote-User-Id` header,
  which a browser can't set, so the check can't be talked around. Find your ID
  in the dashboard's banner while the option is empty, or under **Settings →
  People → Users** (with advanced mode on). Every refused user is logged once.
  Ember records who did what (a grant, a decision, a removed message) as the
  user's display name with their user ID, for example `Stefan (8f14…)`: names
  can be changed, the ID can't.
  The one exception is `/api/sensors`, which can be read from the Home Assistant
  host network (Home Assistant itself, and apps that use host networking). It
  only contains the agent's state, balance, runway and today's spending.
- The app has no access to the Home Assistant API, runs without host
  networking and maps no folders besides its own `/data`. The Supervisor gives
  every app a token for a few Supervisor endpoints about itself (such as its own
  options); Ember removes that token at startup. When Ember's process exits,
  the app stops completely instead of restarting by itself, so every restart
  goes through Home Assistant, which rewrites the options you saved. Turn on
  **Watchdog** so that happens automatically.
- The agent's model calls go only to `https://api.anthropic.com` (any other
  address is refused inside the app, and proxy settings are ignored). Its
  tools have no network access at all. If you set up Ember's mailbox, Ember's
  own code also connects to the IMAP and SMTP servers in the options, over TLS
  with verified certificates, and to no other mail server. If you connect
  Etsy, Ember's code also talks to `https://api.etsy.com`, and nothing else
  there; its tokens are kept in `/data/etsy/tokens.json`, readable only by
  Ember, and never appear in the database, the logs or the diagnostics.
- The agent never writes the bytes of a PDF, Word, Excel or picture file: it
  writes text, and Ember's own code makes the file from it, without any network
  or other programs. Spreadsheets only get formulas with common functions that
  refer to their own cells. The dashboard never opens a product in the browser:
  it shows the product's pictures and downloads the file.
- Workshop code runs only in Anthropic's sandbox (isolated containers without
  internet), never in the app. The files a run made are checked by Ember's own
  code before they are kept (see [The workshop](#the-workshop)), and the
  dashboard offers a workshop script only as a plain-text download.
- The API key and the mailbox's app password are kept in the app options and
  never written to logs, the database or the dashboard (which only says
  whether they are set). Home Assistant backups contain the options, so
  encrypt your backups.

## Data and backups

Everything Ember keeps lives in the app's `/data` folder: `ember.db` (SQLite,
with the emails Ember received and sent), the agent's `workspace` and `memory`
folders, and the same two folders for dry
run under `dry_run` (started fresh with every dry-run session). Home Assistant backups include
it. The app is stopped briefly while a backup is taken so the database is copied
in a consistent state. Before a database upgrade, Ember also keeps a copy in
`/data/backups`.
