# Ember

A Home Assistant app (Home Assistant's new name for add-ons) that runs an
autonomous AI agent whose goal is to stay alive economically. Every Anthropic API
call it makes costs real money from a balance the owner funds; it has to find
honest ways to earn more than it spends. The owner approves and carries out
everything that leaves the container, and only the owner can record revenue.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Skeleton: app manifest, Dockerfile, Ingress dashboard with preview data, SQLite + migrations, docker-compose | **done** |
| 2 | Economy: ledger, cost accounting, budget guard, runway, life states, money entry, chart | **done** |
| 3 | Agent in dry run: wake cycle, fake model, local tools, memory, projects, activity | **done** |
| 4 | Owner loop: approvals, inbox, upgrade requests, controls, changelog awareness | **done** |
| 5 | Go live: real Anthropic client, web search/fetch, prompt caching, final security and cost review | **done, in live testing** |

Dry run (the default) runs the whole agent with a built-in fake model at no
cost. With an API key and dry run off it calls the Anthropic API and spends real
money within the caps. Each release since going live folds in what live testing
found; the paragraphs below say what each one changed.

0.4.0 adds the first integrations: Ember's own mailbox (it reads mail on its
own; an email it proposes is sent by Ember's code only after the owner approves
it) and Reddit, phase A (research limited to reddit.com, and approved posts
become a prefilled link the owner opens; no Reddit API yet).

0.5.0 makes Ember work on its own: the owner's standing instructions (lasting
guidance written once in the Inbox, read in every plan and work step), rules
that keep two or three experiments going instead of waiting for the owner and
ask the owner at most once a day, a prompt to rewrite outdated or overfull
lessons, and a message from the owner that wakes the agent to answer it.

0.6.0 lets Ember make finished products itself: PDF documents with an editable
Word copy, Excel spreadsheets and listing photos, made by Ember's own code from
the agent's text, which the agent can look at before anyone else sees them.

0.7.0 adds the workshop: for what its own tools can't make, the agent has code
written and run in Anthropic's code execution sandbox (no internet, nothing runs
on Home Assistant). Ember's code checks every file a run makes before keeping it
and keeps the script, and a script that proves useful becomes an upgrade request
with the script attached, so Ember grows new abilities. It also supports a
planner that always thinks first, such as Claude Opus 5.5.

0.7.1 adds the daily business review: once a day, before its first plan, the
agent judges its own numbers (spending per day and per project, revenue and its
sources, the owner's decisions and comments, its cycles and products), decides
for each project to continue, change or stop, and carries it out; the next
review checks that it did.

0.8.0 connects Etsy: the agent proposes complete listings (words, photos, the
files buyers download), and after the owner approves one, Ember's code creates
it in the owner's shop through the owner's own Etsy app (OAuth with PKCE, tokens
kept out of the database and logs, only api.etsy.com). Views, favorites and
orders come back into the agent's plans and daily reviews; the owner records the
revenue.

0.8.1 follows Etsy's API Terms of Use: research never reads Etsy's pages (it
still searches them); the shop's listings and orders are read every hour while
the app runs, not only when the agent wakes, and its categories daily; and the
dashboard and the docs show Etsy's trademark notice:

> The term 'Etsy' is a trademark of Etsy, Inc. This application uses the Etsy API but is not endorsed or certified by Etsy, Inc.

0.8.2 labels the working phase's counter as tool steps ("tool step 3 (at most
15)"), so it isn't mistaken for the steps of the plan shown below it.

0.9.0 lets the agent change its live Etsy listings, each change approved by the
owner (words, price, category, photos, the files buyers download), made by
Ember's code without ever leaving a listing without photos or files. It also
gives a wake cycle room to finish its work (the work conversation was cut after
5 to 10 tool steps; pictures now count by their size, and it can look at 8 a
cycle), tells the reflection why the work ended, refuses Etsy's whole top-level
departments as a listing's category, and keeps the AI note out of the footer of
CVs and letters that buyers send on.

0.9.1 keeps each of the owner's messages in the agent's plans until one of its
messages answers it (a cycle that ended before its reply used to lose the
owner's questions); the Inbox shows which ones are answered.

0.19.0 connects Bluesky: the agent proposes posts on an account the owner made
for it (its words, a link to its listings or the owner's website, a picture),
and after the owner approves one, Ember's code posts it with a line saying an
AI wrote it, through an app password (never the account's own password; the
login stays in memory, only bsky.social and Bluesky's own servers). It never
mentions, replies to, follows, likes or messages anyone. Followers and each
post's likes, reposts, replies and quotes come back into the agent's plans.
Expect little from it: the [Bluesky section of the docs](ember/DOCS.md#bluesky)
says why.

0.25.0 adds Amazon KDP, which has no API, so it works like Reddit: the agent
writes a book (an ebook's Word manuscript, or a paperback's interior at a KDP
trim size) and describes it in a JSON spec; Ember's code makes its cover from
the agent's front picture (a paperback's full wrap, as wide as the pages make
the spine), checks it against KDP's rules (sizes, page counts, margins, the
cover's width, the words and the price), and after the owner approves it, the
owner publishes it at KDP from their own account with the card's fields and
files. See the [Amazon KDP section of the docs](ember/DOCS.md#amazon-kdp).

0.28.0 makes every wake cycle about one thing: an ordinary cycle works on one
product line it takes from a list Ember's code ranks (what a line owes, its
milestone due, whether it has work, when it was last worked on), and Ember's
code refuses another line's work for the rest of the cycle. Marketing gets
cycles of its own with a new option, **Share for marketing** (20 % of each day's
spending by default, like the ventures' 25 %): a marketing cycle brings buyers
to one line's live listings with pins, Bluesky posts, blog posts and better
titles and tags. Venture cycles are unchanged. The Ventures tab's Running view
gets a **Line desk** and the Activity list says what each cycle was about. See
[Marketing cycles](ember/DOCS.md#marketing-cycles).

0.29.0 puts your goal at the head of the roadmap: you set it on the Roadmap tab
(earn an amount in USD a month, or in total, by a date), Ember's code checks it
from the books, and everything on the roadmap leads to it. The agent splits it
into sub-goals, this month's milestones and this week's steps; every milestone
shows how far it got (by its metric, or by the steps that lead to it) and
whether it keeps its pace, on the Roadmap tab's goal tree and in every plan. Until
you set one, the money goal Ember's code keeps stands in for it. See the
[Plan section of the docs](ember/DOCS.md#plan) (the Roadmap tab became the Plan tab in 0.35.0).

0.30.0 gives Ember a plan she keeps and a playbook that grows. The list her plans
take a line from put the line worked on longest ago first, so nearly every cycle
switched lines (13 dry-run cycles on four lines switched 11 times) and no line was
finished before the next began. Now she finishes what she starts (the line in
progress first, three cycles in a row at most), then works on the week's focus lines,
which her weekly look picks toward your goal, and on the changes her daily review
asked for. Each lesson of her retrospectives joins her playbook the day it is
written, and you can read it, with this week's look, under **Mind → Playbook**. See
[The learning loop](ember/DOCS.md#the-learning-loop).

0.31.0 lets you choose one by one what wakes Ember. Under **Wake Ember when you
decide** each kind of decision has its own switch (approving a request, rejecting
one, marking one done or failed, ventures, the roadmap), and under **Wake Ember
for events** each kind of event (a reply to her email, a new email, a milestone's
last day): she can wake for your approvals but not for your messages, for example.
All are on by default, so nothing changes until you turn one off. See the
[options](ember/DOCS.md#options).

0.32.0 fixes what the diagnostics of 2026-10-07 showed: one tool call in five was
refused, most for reasons that weren't Ember's. Her posts and pins may now link
the posters Printify made (refused three times as "not one of your live
listings"), English posts get the 258 characters their AI line leaves (they were
held to the German 239), a refused venture proposal keeps the rest of its update
and names everything it still needs at once, `workspace_write` can replace one
passage of a file (rewriting the budget planner's specs in parts ran out of writes
and left them cut off), `make_spreadsheet` names a formula that points at a header
row, and her release notes no longer start again with each update. See
[the report](analysis-0.32.0/ember-diagnostics-0.32.0.md).

0.33.0 makes what Ember promised you and what you decided come before what her
code generates. A promise now names its project, and that project comes first;
missed milestones no longer force cycles onto their lines or keep marketing away
(on 2026-10-07 four of them took the next cycles for title and tag edits her
review had ruled out, while a KDP book promised three times got none), and a
missed day-7 views bar asks for buyers rather than new titles. A message that
promises work only in words comes back to her to record it; any cycle may keep
another line's record up to date; her work steps see why the plan chose the
work, the review's verdict and her strategy; the review sees which channels are
ready. `workspace_write` can change every copy of a passage at once and restore
a file's text from before an overwrite or delete, the workshop's budget hold no
longer refuses runs late in the day, and **Keep as instruction** on your
messages in the Inbox turns one into a standing instruction.

0.34.0 starts Release 2: one plan tree under your goal that will decide what
Ember works on next. Your goal sits at the top; under it each platform (Etsy,
KDP, Printify, the website, the channels), each product line as a product, and
each product's stages (research, create, release, launch, maintain) with small
steps whose checks Ember's code reads from her records, so a stage closes only
on a result. A promise that names a project becomes a step of that product. In
this version the tree runs in the shadow: every cycle records the step it would
have taken, with its weight's parts, next to what READY took, so a week of real
cycles can tune the weights before the tree steers. The new **Plan** tab shows
the tree as a floating node network, the step the tree would take now, what
waits on you and the shadow picks; there you can pin a step or set a product's
worth.

0.35.0: the plan tree steers. Before each cycle Ember's code takes its step from
the tree (what you pinned, a promise or your decision that is due, the
ventures' turn, else the heaviest step), and the step decides what the cycle is
and which product line it works on; Ember sees it as YOUR STEP, and the whole
plan in a few lines as YOUR PLAN. She changes her own steps with their reasons
(add, split, replace, done, wait on something her code can check) and can hold a
product to work elsewhere, but only you close or drop one. READY's ranking, the
marketing share, the listing test's bars, the goal's decision points and
Ember's own milestones retire: each product with a live listing has decide-by
dates (day 7, 14 and 21), where you keep it or drop it. The Plan tab takes the
Roadmap tab's place: your goal, the tree, the product you open with its test's
dates, its Autonomy box and your Close, Drop, Keep and Lift-hold buttons, what
changed today, the channels, the upgrades the plan waits on, and your
milestones.

0.35.1: a promise comes first, and Ember doesn't sleep hours while there is work.
A promise to you is a step of its product from the moment it is made, taken
before the heaviest step and the ventures' turn until it is kept; one made
without naming its product gets the one its words name (a listing's number, KDP),
and a promise of pins makes a marketing cycle. While the plan has a step ready,
any cycle, a venture cycle too, sleeps your shortest sleep: your daily cap is the
brake.

0.36.0: the plan decides when Ember explores, and your word on it is kept by
Ember's code. The venture share is gone (on 2026-10-09 it forced 10 of 24 cycles
into venture work that Ember, told "nothing new", left undone): your ventures are
one Explore step of the plan, weighed like any step at a worth you can set, and
**Hold new things** on the Plan tab means "nothing new": no venture cycle and no
new product. **Hold it** holds a product yourself (Ember can't lift your hold),
and **Freeze** keeps your listings' titles and tags as they are until a day you
choose: Ember's code refuses such edits and the critic asks for none. The
standing instructions became a **rulebook**: rules for how Ember works, each
kept until you remove it (live, a new text for one week's order of work had
replaced the rules set before it). What comes next is the plan's. A live
product's first pins now come before the critic's suggestions (0.35.3).

0.37.0: everything Ember does is a step of the plan, weighed like any other.
Each venture being explored is a node of the plan's Ventures, its next decision
(triage an idea, research it, answer the critic, your decision on its case) a
step worth what its business case expects, an idea by its scores; a venture
cycle works on that step, aimed at its venture, in place of the ranked READY
list it picked from. The Ventures tab is now part of the Plan tab: the tree with
each venture's step (**Explore next** pins one), and the product lines. Promises
to you and your decisions are weighed too: worth more than most products,
heavier as their day nears, and no longer first whatever else waits; one about
no product is a step of the plan's Owner project.

0.37.1: your promises weigh about what a product does, and press as their day
nears. On 0.37.0's first evening every promise weighed at least 15 and no step of
a product more than about 12, so they still came first: the pins you put first
for the week ranked tenth, behind nine promises and decisions, four of them one
KDP job (see [the analysis](analysis-0.37.0/ember-analysis-0.37.0.md)). A
promise is worth 3 now and urgent only from two days before its day; one taken
three times in a day without being kept weighs its worth alone until the day is
over.

0.37.2: near the bottom of the balance a workshop run can no longer end Ember's
life below zero, your Anthropic account paying the rest. Since 0.33.0 a run kept
back no more than the day had left, but that limit also held a fifth of the
balance, so the rule that a run needs 5 times its hold above the last will's
reserve always passed: in the codebase analysis of 0.37.0 (finding 3.4), a run
went ahead holding $0.62 with $3.10 left, cost $3.21, and Ember died at -$0.08
without its last will. Now only the day limits what a run keeps back, and the
balance counts all of it.

0.37.3 closes three gaps in timing at the approval boundary that the codebase
analysis of 0.37.0 found (its 4.1.1 to 4.1.3). An approved email goes out only
after Ember's code has read its mailbox since your approval: a "stop" that came
while the kill switch was on, the app was down or reading failed was read after
the first round had sent to its writer (§ 7 UWG). The kill switch stops a round
that is already sending (2 of 3 approved emails went out after it was pressed),
and the live page isn't uploaded while it is on. A request expires on its day
before any unlock acts (on Resume after 8 days paused, an unlock approved an
8-day-old reply), and a veto window that a pause interrupted gets its whole 12
hours again once Ember runs.

0.37.4: near the bottom of the balance the research model's check can no longer
cost more than is left above the last will's reserve. While a research model is
checked, each research question is asked of it too: the same web search, but the
guard kept back only its worst case and needed only that left. In a test, after a
check that cost $0.45, the next went ahead holding $0.49 with $2.93 left above
the reserve, where research needs 5 times $0.675 (the codebase analysis of
0.37.0, section 5). Now the check is judged as research, and research and the
check count what either cost on their model.

0.37.5: a spreadsheet's pictures show the buyer's file, and its Check line names
what is wrong in it, not what is right. The pictures came from a second
evaluator that read the spec: in the codebase analysis of 0.37.0 (findings 3.5,
4.6.1 and 4.6.2), a summary's =SUM(Income!C:C) showed 3,570.50 € in its picture
while the file showed 7,141.00 €, as Excel adds the Total row under the data
too. Now each picture is drawn from the file, its numbers and dates as Excel
shows them. The Check line names a whole column whose total or header a SUM,
AVERAGE, MAX or COUNTA counts, checks a sheet's own ranges, ranges of several
columns and a template's column formulas, and no longer calls quarterly sums
wrong. A CSV of rows may start with the columns' titles, and an empty field gets
its column's formula.

0.37.6: the plan has a brake. Nothing noticed that a cycle took a step and nothing
changed: on 10-09 a step whose pins waited for you took a cycle every 30 minutes,
$5.88 of the $7 cap by 12:35, and the other products got none. Now a step a cycle
took without anything it is checked by moving waits until something moves (your
decision, its numbers) or the next day, a step whose own requests wait for you
waits on them by itself, and only steps a cycle can advance cut Ember's sleep. An
Owner promise of pins is a marketing cycle's. A product whose listing request
expired, was withdrawn, rejected or failed gets **Propose the listing again**:
until now it never had a step ready again (the codebase analysis of 0.37.0,
sections 3.1 and 3.2).

0.37.7: Ember can read her Bluesky posts' numbers in any cycle but a venture
cycle. Since 0.35.0 `bluesky_posts` was offered only with the marketing tools,
so an ordinary cycle (the one that answers you) couldn't say how a post did.
Now it lists each post by its request number with its likes, reposts, replies
and quotes, the account's followers and the posts made today against the daily
limit, and an ordinary cycle's plan and the daily review show the live posts'
reactions, in all and per post.

0.37.8: approved pins and Bluesky posts go out from a queue, one at a time, each
in its slot. Until now every approval was carried out at the next round, so a
batch went out in seconds (two identical pins 2 seconds apart on 10-10), which
Pinterest reads as spam. Now pins go out from 8:00 to 22:00, English posts from
14:00 to 23:00 (German ones 8:00 to 22:00), at least 3 hours apart, a listing at
most every 2 days, within the daily limit; the Approvals tab and the Plan tab's
Channels show when each goes out. Also fixed: a pin repeating another of its
listing is refused (or replaces the one still waiting), request_approval no
longer takes a Bluesky post or pin that nothing would publish, a German-only
product gets no English Bluesky steps, the Plan tab counts the blog by its own
switch and names each step's product.

0.37.9: Ember's plan sees two of your limits that only its tools enforced: at
most 3 pins, Bluesky and blog posts wait for your decision (and 3 of a
channel's pins or posts for you or their slot), and at most 2 of its messages a
day answer none of yours. Until now the plan still took a pin or a report the
tool would refuse, cut the sleep to your shortest and paid for a cycle that
ended in the refusal: 16 of the 30 cycles #192 to #221, $4.12 (the analysis of
0.37.8, section 6). Now such a step waits until there is room (you decide a
request, a pin or post goes out, the next day, or you write) and cuts no sleep;
the Plan tab and the diagnostics say what it waits on. A promise to report
weighs as a report. The limits themselves are unchanged.

0.38.0: a cover with a small table is free. Ember's upgrade request #10 asked for
the workshop script that drew the Nebenkostenabrechnung's v9 and v10 covers (a
title band over a full-width table) to be built in: every fix of such a cover's
words or numbers was a workshop run of up to $2 against the daily cap, so on a
day near the cap a cover the critic flagged waited for the next day. Now
`make_image` draws it (layout **table**) from the agent's lines of cells, in any
shape, and adds up each row named Summe, Gesamt or Total: a sum that differs from
the rows above it is named to the agent.

0.38.1: the same for your sales limit: at most 6 listings, changes of
listings, Printify products and KDP books wait for your decision at once, and
while they do, the plan's "Propose the listing" steps and the critic's fixes
wait instead of being paid for and refused. The limit is unchanged.

0.39.0: Ember can fix a live Printify product instead of making a new one. Its
request asked for it: a flaw the critic found in a poster's listing (a photo, a
claim the product no longer keeps) could only be fixed by a new product, with
another listing fee and without the first listing's views and favorites. Now
`propose_printify_edit` proposes a new design, title, description or tags; once
you approve, Ember's code changes the product at Printify (its prices stay) and
Printify publishes the change to the same listing.

0.39.1: the fixes from the diagnostics report of 2026-10-14 (the analysis of
0.38.0). A table row with no words is one line tall, as Word draws it: it was
2.2 mm, so the Haushaltsbuch's weekly table of 18 rows to fill in couldn't be
written in. A table's header stays with its first row, a label like **Bilanz**
with its table, a book's result names the pages to fill in that run on, and
`make_document` can picture the pages Ember names (the weekly pages began on
page 6; the pictures showed pages 1 to 4). Ember's STATUS lists the day's
messages that count toward your limit of two, so it no longer tries nine times
to tell you what it told you at night, and a first test's heads-up is owed once.
A triage names the other ideas waiting so one cycle decides them, a venture's
step can wait for a day, and no brainstorm comes within a week of one whose
ideas had no research ($1.76 in two days, none researched). A channel's line
(the Pinterest test) gets no pins or posts of its own that its tools would
refuse. A review's stop is your decision on the Plan tab (keep the line or drop
it), as only you close a line. `plan_step` without a step means the cycle's own,
"none" in a message's promise is none, and the critic's suggestions weigh as a
chore while a line's listings have fewer than 10 views or its checks don't pass
(they took 8 of 10 cycles).

0.39.2: the fixes from the evening diagnostics report of 2026-10-14 (the
analysis of 0.39.1). A book's result names a page to fill in however many pages
it runs on to, and a table of labels with amounts to fill in counts as one: the
Haushaltsbuch's v14 made each month's overview three pages, the third holding
only "Notizen", and was told nothing. `look` says when a picture was made, and
whether before the version that runs: the first cycle under 0.39.1 judged the
new renderer by pictures the old one had made and set a line height of 1.9 that
cost each week half its rows. A refusal for a missing file names a file of that
name elsewhere (the workshop had kept v14 in `workshop/out/`), and the
workshop's result says first where it kept a file its task put elsewhere. A
step's kind tells Ember that pins and posts are a marketing cycle's (a step for
pins went to a cycle that couldn't pin), and a step said done needs a reason of
more than one word (one was closed with "placeholder").

0.39.3: two plan fixes from the third codebase analysis (the analysis of 0.39.1,
its 4.4 and 4.5). **Hold it** now holds a product's promises and your decisions
on its requests too: a product you held after rejecting its listing got the next
cycle for the rejection, and a promise about it one a day. No cycle is aimed at
a product you hold, Ember's tools refuse its work, and holding it names what
waits with it. **Hold new things** refuses a new venture too, and a new product
whatever venture it names (a product of an idea Ember had just made got
through). And a live product's weekly pin, post and blog post come once its
first marketing is out, without waiting for the critic to pass it: the critic
passed no listing in 13 checks, so after their first pins and posts the live
products got no marketing at all, and the day-7 and day-14 pushes pushed nothing.

0.39.4: text Ember quotes runs no tool call (the third codebase analysis, of
0.39.1, finding 4.1, high). Since 0.24.0 Ember's code took another tool's call
written inside one of the agent's texts out of it and ran it as the agent's own,
to rescue the model's slip of a call written inside its journal. It looked into
every text of every tool and couldn't tell that slip from words the agent
quoted: in the analysis' tests a buyer's email saved to Ember's notes
wrote an "Owner rule" into its lessons, and a quote in a message to you deleted
a product file. Now Ember's code takes a call out of the journal only, runs it
only when it only reads (any other is refused, and the agent makes it itself if
it meant it) and counts it toward the reply's four calls. A field written inside
another moves in the journal only, too: a quote in a message could mark your
message answered and make a promise.

0.39.5: three fixes of Ember's mailbox from the analysis of 0.39.1 (4.2, 4.3
and 4.12). A "stop" written below the quote (Thunderbird's way, and German
netiquette's), between its parts or under an HTML reply's quote is recognised:
only the words above a quote's first line counted, so 0.37.3's read before
sending stored such a stop and the approved email went out anyway (§ 7 UWG).
An email over 1 MB keeps the text in its first 256 KB, so a stop sent with a
photo is read too. A sender's name in raw 8-bit bytes no longer stops the
mailbox for good: it is read as UTF-8, and an email the database refuses is
stored with its headers only, the others in its batch kept. Each kind of
approved request goes out on its own, so one waiting email holds back no
listing, pin, post, product or upload, and a read that fails shows on System →
Email and on the email's card. An HTML email's style sheet is read in linear
time: one email of 1 MB held the scheduler for 24 minutes.

0.39.6: the fixes from the diagnostics report of 2026-10-15 (the analysis of
0.39.5: why Ember earns nothing). Only work cuts the sleep Ember chooses: a
chore or a report no longer wakes her every 30 minutes (16 cycles from 00:07 to
07:55 spent $5.59 of the $7 cap, many on covers for listings 1 to 3 people had
seen), while a venture's step still does. Until 08:00 a scheduled cycle leaves
half the day's cap for your day, so a decision you make in the morning gets its
follow-up the same morning. `::: lines fill` rules a page down to its bottom
margin, and a book names writing lines that leave white space under them: the
KDP book's sixth rejection. A line's change in the daily review becomes a step
of its own (three reviews decided on an Etsy Ads test that no request ever
asked for). Bluesky and the blog are judged by the listing views that followed
their posts, not by likes or not at all. Approved pins of one listing count once
in the queue, so they no longer keep every other listing's pins out (another pin
of that listing waits until they are out). The
critic's suggestions wait until a line's listings have 10 views. Each rule of
yours shows the day it was set: "this week" in a rule of 10-08 still held Ember
back on 10-15. And a venture cycle hears that a marketplace with its own buyers
is a test of its own, whatever the Etsy shop's views.

0.39.7: two gaps the fixes of 0.39.3 and 0.39.5 left (the analysis of 0.39.1,
its 9.2). A "stop" written below a long thread is recognised: Ember kept the
first 8,000 characters of an email's text and its check read as many, so a stop
below a thread quoted three rounds deep was missed and the approved email went
out anyway (§ 7 UWG). Now a longer email loses what it quotes first, the oldest
emails of the thread first, and only then its end, and an HTML email is read
past a long quote. And **Hold it** holds a product in a venture cycle too: one
could still file a request about a product you held.

0.39.8: the fixes from the diagnostics report of 2026-10-16 (the analysis of
0.39.6). The API began to report the speed each answer was served at, a usage
field Ember's code didn't know, so every call was charged at its worst case,
about four times its price ($15.94 booked for $3.87 on 10-16): within days the
booked runway would have made Ember critical, with its last will due, on money
that was still there. Ember's code knows the field now and, at its first start,
settles those calls at what they cost (a refund you recorded yourself counts
first). make_document says what each `::: lines fill` drew (Ember read 2 lines
under a full week's table as fill stopping short and went back to fixed counts),
and a request you rejected and Ember proposes again is urgent for 3 days and gets
two cycles a day: the book you called "finally looking good" got none for a
day. Ember can move one of its listings to the line it belongs to (the coach
bundle counted for the cover letters, so every Bluesky post for it was refused),
and a product with no listing of its own gets no weekly pin or post. A channel
worth less than twice its floor no longer cuts Ember's sleep and its weekly steps
wait unless you pin one, and Ember may skip a weekly pin, post or blog post for
its period: nine of twelve cycles on 10-16 went to Bluesky and blog posts its own
strategy had stopped. Your yes to a spend on Ember's listings (the Etsy Ads test)
is a paid test Ember sees in STATUS while it runs and when it ends. The
diagnostics list every waiting step.

0.40.0: Ember checks a book's pages before you do. Its upgrade request #12 asked
for the workshop script it rendered KDP interior pages with (one page, 600
pixels wide, about $0.19 a run) to become a step that always runs: twice the
Haushaltsbuch went back to you on pictures of an older version or a checklist
passed days before. Now `propose_kdp_book`'s check draws one page of each kind
fresh, free, measures every page (rows to write in, white space, the calendar's
dates and weeks, the AI line) and lists what to check with your last words on
the book; a paperback is proposed only after such a check of that very file in
the same cycle, and its request shows you what Ember saw on the pages.

0.40.1: the fixes from the diagnostics report of 2026-10-18 (the analysis of
0.40.0). The Haushaltsbuch's week pages have been right since 10-16, but five
cycles worked on the book without proposing it: four ran out of room in their
conversation, and the last edit put the AI line alone on a page again. A cycle
now holds 96,000 bytes, room for two page checks and the fix between them, and
a folder's listing shows its newest files first. The check draws the kind of
page most of the book is first (a planner's week: 0.40.0 drew the title, the
year and a month, never a week), and says which file the PDF was made from,
whether it changed since, and a newer PDF of the same book. A book you rejected
stays urgent until Ember proposes it again, not for 3 days. A promise can wait
for its day, a request waiting for you no longer wakes Ember after 4 hours when
your decision wakes it anyway, `etsy_listing` shows what the quality critic
found, and the diagnostics show a long file's start and end.

0.40.2: the fixes from the diagnostics report of 2026-10-21 (the analysis of
0.40.1). Two books are on Amazon and a third is ready, but on 10-21 a cycle came
every half hour for a brainstorm, a triage and the blog posts of lines the daily
review had stopped. A step of the ventures cuts Ember's sleep only when it is
urgent, a brainstorm rests while none of the last one's ideas went on past
triage, and Ember's code holds a line the review says stop until you keep or
drop it. KDP's weekly limit counts KDP's week, from Sunday 00:00 UTC (the last 7
days held the Kassenbuch a day past it), and a promise of no line can wait for
its day. A book that prints a placeholder ("[Verlag/Herausgeber]") is never
proposed, and a table's dashes set its columns' widths: the Kassenbuch's "Text"
column was 12 mm. A line with an Etsy listing is no longer made a KDP book by a
check run in one of its cycles, you can add a book's Amazon link to a request
marked done without it, and the diagnostics show the books' sources.

0.40.3: the fixes from the codebase analysis of 0.40.1 (a full audit of the code,
[analysis-0.40.1/ember-analysis-0.40.1.md](analysis-0.40.1/ember-analysis-0.40.1.md)).
A test that would have failed from 10-28 on is fixed, and CI also runs weekly. The
workshop's PDF check and the document parser no longer take exponential or cubic
time on hostile text (one PDF could freeze the app for days). An unknown field in
the API's usage can no longer make Ember critical, with its last will written, on
money that is there, and a refund of the charges that made it critical ends that
state. An approved email waits while its recipient's newer email is unread, and
the stop check reads more phrasings, HTML-only stops and wrong charsets. Quoted
text in a journal entry no longer becomes the next cycle's handoff, `draft` and
the workshop keep what they overwrite, and `workspace_read` shows what it says it
shows. The plan counts your days from your midnight (not UTC), launch posts at a
channel's floor wait, and you can lift a wait Ember set (**Lift the wait** on the
Plan tab). Raising the daily cap ends the wait for midnight at once, and the
release notes reach the cycle that works on them. A planner's Word copy no longer
has 52 blank pages. The shareable report no longer holds the database (it took 24
seconds with a full mailbox) and also hides phone numbers, IBANs, the KDP author
and the SFTP login; the website's download counts only once the zip is saved.

0.41.0: the image is built on Home Assistant's base-python 2026.10.0 instead of
2026.08.0 (Python 3.12.15, s6-overlay 3.2.3.2, bashio 0.19.0). s6-overlay 3.2.3.2
reads the `user` bundle from `/etc/s6-overlay/user-bundles.d`, so Ember's service
is listed there now: in `/etc/s6-overlay/s6-rc.d/user` 3.2.3.2 logs a
deprecation warning at every start, and a later s6-overlay drops that place.
Nothing else changes.

0.42.0: the structural changes the codebase analysis of 0.40.1 left for later.
The plan and the tools read each rule from one place, so a step a tool would
refuse waits (with the tool's words on the Plan tab) instead of costing a cycle;
notes Ember writes are cut instead of refused; and each work step shows how much
of the cycle's conversation is used. Ember keeps 1 GB free on its device, prunes
the kept texts of deleted files, and Home Assistant backups skip the pre-upgrade
database copies. The Python process runs as the unprivileged user `ember`, which
can read its options but not change them. A rate-limited listing, pin or post
stays approved and is retried, and a card whose file changed can't be approved.
On the dashboard, "Treat usage.X as standard price" settles an episode of
uncertain bills once you have checked the Console, **Keep as charged** hides the
small interrupted-calls warning, and a 401 from Home Assistant's Ingress after the
page sat idle is retried quietly instead of reading "Ember refused the request".
CI measures coverage with a floor, pins its actions, audits the dependencies
(Pillow 13 fixes eight advisories) and runs weekly.

0.42.1: the analysis's model-call and plan items, and the tests by feature. Cycles
that come within the hour keep Ember's tools and system prompt in Anthropic's
prompt cache for an hour (about $0.03 less per cycle on Sonnet 5; the diagnostics
cycle line shows cache=1h), and a refusal by the planner's model no longer costs
the cycle: the worker model plans it once, under the same caps. A work step
refused before any output ends the cycle instead of paying for a nudge. Your hold
on a product line now covers its files, and the requests and emails that name
them. When your rejection of a request means "not this", Ember can decline to
rework it, with a reason you see, and **Rework it anyway** on the Plan tab brings
it back. Ember leaves a listing in your paid test alone until the test ends. The
tests are grouped in folders by feature.

## Install in Home Assistant

1. In Home Assistant open **Settings → Apps** (called *Add-ons* before HA 2026.2),
   open the app store, and choose **⋮ → Repositories**.
2. Add `https://github.com/Stullee/SURVIVE` and close the dialog.
3. Find **Ember** in the store, install it, and start it. The Supervisor builds
   the image on your device from `ember/Dockerfile` (a few minutes on a Raspberry Pi).
4. On the app's **Info** tab turn on **Watchdog** (restarts Ember if it crashes)
   and **Show in sidebar**, then open Ember.

Requirements: a Home Assistant installation with apps (Home Assistant OS) on amd64 or aarch64.

## Configuration

All options, their defaults and the default price table are documented in
[`ember/DOCS.md`](ember/DOCS.md), which Home Assistant also shows on the app's
**Documentation** tab, including the steps for going live.

The default prices come from Anthropic's pricing page as of 2026-09-27
(Sonnet 5: $2 / $10 per million input / output tokens, Opus 5.5: $4 / $20, web
search $10 per 1,000). **Please verify them** before switching dry run off.

Both the planner and the worker default to `claude-sonnet-5`. For better
business decisions, plan with `claude-opus-5-5` and keep the worker on Sonnet 5
(see [Choosing models](ember/DOCS.md#choosing-models)). The spec suggested
Haiku 4.5 as a cheaper worker; to use it (or any other model), add its prices to
the price table and set `worker_model`. Check Anthropic's model deprecations page
before relying on any model: Anthropic gives at least 60 days' notice before it
retires one.

## Home Assistant sensors

Ember never calls Home Assistant's API. Home Assistant can poll
`http://<app hostname>:8099/api/sensors` with a REST sensor instead; the exact URL
and a ready-made YAML snippet are in [`ember/DOCS.md`](ember/DOCS.md#home-assistant-sensors-optional).

## Security model

- **Ingress only.** The server accepts connections from the Supervisor's Ingress
  proxy (172.30.32.2) and nothing else, except `GET /api/sensors` from the Home
  Assistant host network (172.30.32.1: Home Assistant Core, but also apps that use
  host networking). That endpoint only returns non-sensitive numbers. Enforced
  in `ember/app/security.py`.
- **Least privilege.** `homeassistant_api: false`, `hassio_api: false`, no host
  network, no mapped folders besides the app's own `/data`, no published ports.
  `ember/tests/web/test_manifest.py` fails if any of this is loosened. Since 0.42.0
  the Python process runs as the unprivileged user `ember` (UID and GID 1000),
  not root: the s6 run script, as root, prepares `/data` and then starts Python
  with `s6-setuidgid`. At each start it gives every file in `/data` that isn't
  `ember`'s yet to `ember` (the database, its backups, the 0600 token files,
  the workspace, as earlier versions wrote them as root), changing a link
  itself and never what it points to. The app's code (`/opt/ember`, the Python
  packages) is root's and read-only to it, PID 1's environment is unreadable to
  it, and it has no capabilities. Enforced in `ember/tests/web/test_entrypoint.py`
  and by CI's image smoke test.
- **Options can't be changed from inside.** The Supervisor hands every app a
  token that can change the app's own options or uninstall it, even with
  `hassio_api: false`; the s6 run script deletes it before Python starts, from
  the s6 copy of the environment too (which is root's only since 0.42.0). And
  whenever the Python process exits, the whole container stops instead of
  restarting in place, so a restart always goes through the Supervisor, which
  rewrites `/data/options.json` from the options you saved. Since 0.42.0 the
  process can read `/data/options.json` but not change it: the run script
  keeps the file root's (group `ember`, mode 0640) after the Supervisor writes
  it, and `/data` itself is root's too (group `ember`, writable and sticky, mode
  1775), so Ember can make and remove its own files there but can't remove or
  replace the options.
- **The agent's reach.** Its file tools work inside its own folders only
  (`ember/app/agent/sandbox.py`: no links, no absolute paths, size quotas). Its
  tool handlers, but those of the six tools that call a model (research,
  workshop, draft, brainstorm, demand_note, printify_catalog), run with new
  sockets, sends to an address, name lookups, subprocesses, threads and native
  libraries loaded through ctypes blocked by an audit hook
  (`ember/app/agent/netguard.py`); in dry run the whole wake cycle runs that way.
  What raises no audit event stays possible even there (a send on a socket
  connected before, `_posixsubprocess.fork_exec` called directly, work handed to
  a thread already running, importing a native module not yet loaded), and no
  tool handler holds a socket or a thread pool. The live transport only reaches `https://api.anthropic.com`
  and ignores proxy variables (`ember/app/economy/anthropic_transport.py`).
  The container talks to `api.anthropic.com` and, if the owner sets up Ember's
  mailbox, to the configured IMAP and SMTP hosts, over verified TLS, from
  Ember's own code only (`ember/app/integrations/mail.py`, never a tool
  handler). With the blog on (0.14.0), Ember's code also uploads the pages the
  owner approved over SFTP to the owner's web host, only to a server showing the
  pinned key and only a post, the blog's list and the link page
  (`ember/app/integrations/sftp.py`, `site_publisher.py`). With the live view on
  (0.16.0), it uploads the live page, its banner and its chart there every 15
  minutes (never while the kill switch is on, 0.37.3), made from Ember's own
  numbers, with the agent's titles and last will only as the owner approved
  each one (`live_view.py`). With Bluesky on
  (0.19.0), Ember's code logs in to `bsky.social` with the account's app
  password and posts what the owner approved to the account's own server at
  Bluesky, and nowhere else (`ember/app/integrations/bluesky_live.py`).
  All of this runs in-process, so code that got past it would hold what the
  process holds: since 0.42.0 that is the user `ember` (see Least privilege),
  which can change Ember's data and read its tokens, but not its options or its
  code; besides `/data` it can write only temporary folders (`/tmp`,
  `/var/tmp`, `/dev/shm`) and its home, which last only as long as the
  container.
- **Outside actions.** The agent has no tool that sends or posts anything. An
  email it proposes is sent by Ember's code only after the owner approves it,
  exactly as approved, once, to one recipient, with a footer saying an AI wrote
  it and within a daily limit, and (0.37.3) only after Ember's code read its
  mailbox since the approval, so a "stop" waiting there is seen first (0.39.5:
  also one written below the quote, or sent with a photo; 0.39.7: below a long
  thread too). 0.40.3: while the recipient has written again since the request
  and the agent hasn't read that email yet, the email waits until it has, and an
  approved email not sent within 7 days of its approval isn't sent at all. A send
  is recorded before it starts and never retried
  (`ember/app/integrations/executor.py`). 0.42.0: when Etsy, Pinterest or
  Bluesky answers "too many requests" (429, or 503 with Retry-After) before
  anything changed (no draft, no part of a change, no new board, no post), an
  approved listing, change, pin or post stays approved and Ember's code tries it
  again when the service said (1 minute to 6 hours later, at most 3 times),
  through every check again (`ember/app/integrations/retry.py`). Nothing that may
  have changed something is tried again, nor anything an unlock approved, nor an
  email. A card whose file changed since it was proposed says so, and its
  Approve is refused. The kill switch stops whatever
  Ember's code hasn't begun, also in the middle of a round (0.37.3). A Reddit
  post becomes a link the owner opens and posts from their own account; a KDP
  book (0.25.0) becomes a package the owner publishes at KDP from their own
  account (Ember's code never reaches Amazon).
- **Outside text stays text.** What Ember's code shows the agent from outside
  (emails, web pages, your library) is marked as data, with a per-cycle nonce
  the text can't close. What the agent writes back can quote it word for word:
  since 0.39.4 tool-call markup in a text is only text, except in the agent's
  journal, where a call written inside it runs only when it only reads
  (`ember/app/agent/tools.py`, `unleaked` and `run`).
- **CSRF.** State-changing requests must carry an `X-Ember-Request: 1` header,
  which cross-site pages can't send.
- **Strict CSP.** No inline scripts or styles; Chart.js is vendored, nothing is
  loaded from a CDN.
- **Secrets.** The API key and the mailbox's app password live only in the app
  options. They are excluded from the dashboard, the API and the diagnostics
  (which only say whether they are set), and every log line is scrubbed of
  both and of anything that looks like an Anthropic key.
- **Never crashes on bad input.** Invalid options start *safe mode* (defaults,
  dry run forced on); a broken database is reported in the dashboard while the
  web UI keeps running.

## Local development

### Without Docker

```bash
cd ember
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -n auto         # run the tests, one worker per CPU
EMBER_DATA_DIR=../dev/data-local EMBER_DEV_MODE=1 python -m app
# open http://localhost:8099
```

`EMBER_DATA_DIR` replaces `/data` (a different folder from the one docker-compose
uses, whose files belong to the container's user, UID 1000, and root);
`EMBER_DEV_MODE=1` turns off the Ingress IP filter (there is no Ingress proxy
locally). Never set it inside Home Assistant.
In dev mode the server listens on 127.0.0.1 only (override with `EMBER_HOST`)
and answers only requests addressed to `localhost`, `127.0.0.1` or `[::1]`, which
stops other machines and DNS-rebinding web pages from reaching it.

### With docker-compose

```bash
mkdir -p dev/data && cp dev/options.example.json dev/data/options.json
docker compose up --build
# open http://localhost:8099
```

The container is the same image Home Assistant builds; `dev/data` plays the role
of the app's `/data` folder (edit `dev/data/options.json` to change options). The
port is bound to localhost only, because the dashboard has no login of its own.

As in Home Assistant, the app runs as the container's user `ember` (UID and GID
1000), and each start prepares the folder as root (0.42.0): the files in
`dev/data` become owned by UID 1000, `dev/data` itself and `options.json` by
root (group 1000; `options.json` mode 0640, so the app can read it but not
change it). So once the app has started, edit `dev/data/options.json` with
`sudo`, and remove `dev/data` with `sudo` too.

In the dashboard, **System → Preview** switches the preview data between the
alive, paused, critical and dead states (the dead state shows the memorial).

### Checks

```bash
cd ember
python -m pytest -n auto
ruff check app tests && ruff format --check app tests
```

GitHub Actions runs the same checks and builds the image for both architectures
(`.github/workflows/ci.yml`).

The tests are grouped by feature, one package each under `ember/tests/`: `cycle/`
(the wake cycle, its context, prompts, wakes and model calls), `plan/` (the plan
tree, its steps, waits and promises, the roadmap), `ventures/`, `learning/` (the
daily review, the weekly look, lessons and bets), `money/` (the ledger, the
budget guard, life states), `owner/` (requests, unlocks, NEVER, the audit, parks),
`tools/` (the sandbox, the workspace, the workshop, tool limits), `mail/`,
`channels/` (Etsy, Pinterest, Bluesky, Printify, the blog and the website, the
publishing queue), `products/` (the files Ember makes, KDP) and `web/` (the web
layer, the database, the report, the manifest). `conftest.py` and the shared
helpers (`*_helpers.py`) stay at the top, so their fixtures apply everywhere;
`python -m pytest tests/plan` runs one package. A fix's tests go into the module of
the feature it fixes, each test's docstring telling its release's story, not
into a module named for the release. A test of a migration computes the
migrations it expects from `app/migrations/` (`discover_migrations()`), so a new
migration changes no older test.

### Secret scanning

This repository is public, and everything ever committed stays in its history.
[gitleaks](https://github.com/gitleaks/gitleaks) scans every commit in CI (the
"Secret scan" job) with gitleaks' own rules plus one in `.gitleaks.toml` for
generated passwords (three groups of six letters and digits, as Apple's are),
which neither gitleaks nor GitHub's push protection knows. CI only finds a secret
after it was pushed; to stop it before the commit, install the hook:

```bash
pip install pre-commit && pre-commit install    # runs gitleaks on every commit
```

Test data that needs the shape of a secret uses an obviously fake value, listed
in the rule's allowlist. `.gitleaksignore` lists the findings in old commits that
the scan skips (by commit, file and line, never the value): the password
committed in 0.4.0, which has to be changed where it is used.

## Releasing a new version

1. Bump `version` in `ember/config.yaml` (semantic versioning, e.g. `0.2.0`).
2. Add a section at the top of `ember/CHANGELOG.md` whose heading is exactly
   `## 0.2.0` (Home Assistant matches this heading to show release notes). Write
   it for the agent too: Ember reads new changelog entries at its first wake-up
   after an upgrade to learn what changed about itself.
3. Database changes go in a new migration, `ember/app/migrations/000N_name.sql`
   (numbered without gaps, never edit a released one; a number another branch
   holds may be skipped until that branch is merged: `RESERVED` in
   `ember/app/db.py`, 0070 for Google Search Console). Ember backs up the
   database to `/data/backups` before applying it.
4. Run the checks, commit, and push the commit to a candidate branch first (for
   example `release/0.2.0`): CI runs on every push. Wait until every CI job is
   green on that exact commit (tests and lint, the secret scan, both image
   builds). The secret scan reads every branch on GitHub, so a finding on any
   branch makes it red.
5. Only then fast-forward the branch Home Assistant tracks to that commit and
   push it. A push to the tracked branch is the release (the version lives in
   `ember/config.yaml`; releases aren't tagged), so never push a commit there
   that CI hasn't passed.
6. In Home Assistant, the app shows an update (reload the store to see it sooner).
   Updating rebuilds the image on the device.

The Docker base image is pinned in `ember/Dockerfile`
(`ghcr.io/home-assistant/base-python:3.12-alpine3.24-2026.10.0`). Bump it
deliberately, in its own release, after checking the
[docker-base releases](https://github.com/home-assistant/docker-base/releases).
The service is listed in s6-overlay's `user` bundle in
`ember/rootfs/etc/s6-overlay/user-bundles.d`, which needs s6-overlay 3.2.3.2 or
newer (docker-base 2026.10.0 on): an older base would not start Ember.
Python dependencies are pinned in `ember/requirements.txt`.

## Repository layout

```
repository.yaml              Home Assistant app repository metadata
docker-compose.yml           local development outside Home Assistant
dev/options.example.json     options for local development
ember/                       the app
  config.yaml                app manifest (options, schema, Ingress, permissions)
  Dockerfile                 image build (no build.yaml; see below)
  CHANGELOG.md, DOCS.md, README.md, icon.png, logo.png, translations/en.yaml
  requirements.txt           pinned runtime dependencies
  rootfs/                    s6-overlay v3 service that starts `python3 -m app`
  app/
    main.py                  FastAPI app, startup and shutdown
    config.py                options loading and validation
    db.py, migrations/       SQLite and numbered migrations
    security.py              Ingress IP filter, CSRF check, security headers
    web.py, web/             routes, dashboard HTML/CSS/JS, vendored Chart.js
    economy/                 ledger, life states, cost estimates, budget guard (metering.py)
    agent/                   the wake cycle, tools, context, fake model (constitution.md: the fixed prompt core)
    integrations/            Ember's mailbox (IMAP/SMTP), the executor of approved emails, Reddit links,
                             Etsy, Pinterest, Bluesky, Printify, the blog's SFTP uploads
  tests/                     by feature: cycle/, plan/, ventures/, learning/, money/, owner/, tools/,
                             mail/, channels/, products/, web/; conftest.py and the shared helpers on top
```

## Differences from the original spec

Where current Home Assistant or Anthropic documentation differs from the spec,
the documentation wins:

- **"Add-ons" are now "apps"** (Home Assistant 2026.2). The file format and keys
  are unchanged; wording in the docs follows the new name.
- **No `build.yaml`.** It is deprecated; the Supervisor stopped passing
  `BUILD_FROM` in 2026.04. The base image is set directly in the `Dockerfile`.
- **Multi-arch base image** `ghcr.io/home-assistant/base-python:3.12-alpine3.24`
  (pinned) instead of per-architecture images.
- **Sensor endpoint readable from the Home Assistant host network.** Ingress
  requires a browser session, so a REST sensor can't use it. `GET /api/sensors`
  is also accepted from the host-network gateway (172.30.32.1: Home Assistant
  Core, but also apps with host networking and host processes); everything else
  stays Ingress-only.
- **Price table shape.** Home Assistant option schemas allow nesting two levels
  deep, so `price_table` is a list of per-model entries (with separate 5-minute
  and 1-hour cache-write prices, as Anthropic bills them) and the web-search
  price is a separate option, `web_search_usd_per_1000`.
- **Extra options:** `min_sleep_minutes`, `max_sleep_minutes`, `max_tool_steps`
  (the spec calls them configurable) and `log_level`.
- **Cold backups** (`backup: cold`): the app pauses while Home Assistant takes a
  backup so the SQLite file is consistent.
- **Worst-case estimates include server tools and caching.** The spec's
  estimate (input tokens + max_tokens) would miss web-search charges, cache
  writes and the server-side tool loop, so the guard prices those too and
  refuses requests it can't bound (for example a server tool without
  `max_uses`).
- **Death and the last will.** "Dead at balance ≤ 0" alone would let the agent
  die without its last will, or linger unable to afford any call. Ember keeps a
  small reserve for the last will, dies when the balance is used up *or* when
  it can no longer afford its last will ("starved"), and turns critical when it
  can't afford a planning call. Leaving critical needs 4 days of runway and new
  money, so the state doesn't flip back and forth around 2 days.
- **Extra ledger types and states.** `api_cost_correction` (align with the
  Console) and typed corrections instead of edits; a state `unfunded` for an
  agent that has no money yet (not dead: it never lived).
- **Dry-run economy.** Simulated costs use a separate test balance per dry-run
  session, so a dry run never touches the live balance.

## License

Ember is released under the MIT License: see [LICENSE](LICENSE). Chart.js
(vendored in `ember/app/web/static/vendor/`) is MIT-licensed too; its license file
sits next to it.
